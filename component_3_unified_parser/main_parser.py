"""Unified Parser router entrypoint.

This module routes standardized ECS logs to the selected parsing methodology
(LogParser-LLM, LogBatcher, or LibreLog).
"""

import os
import glob
import json
import argparse
import yaml
import logging
import sys
import time

# Align search path for import resolution of core.* modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tqdm import tqdm
from core.logparser_llm.tree_router import PrefixTree
from core.logparser_llm.llm_extractor import LLMExtractor
from core.logparser_llm.template_manager import TemplateManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("unified_parser")

def format_duration(seconds):
    """Formats a duration in seconds into a human-readable HH:MM:SS or MM:SS format.

    Args:
        seconds (float): Duration in seconds.

    Returns:
        str: Formatted duration string.
    """
    if seconds is None or seconds < 0:
        return "0s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s:.1f}s"
    elif m > 0:
        return f"{m}m {s:.1f}s"
    else:
        return f"{s:.1f}s"

def load_config(config_path='/app/config.yaml'):
    """Loads centralization configuration parameters.

    Args:
        config_path (str): YAML file path. Defaults to '/app/config.yaml'.

    Returns:
        dict: central YAML configuration.
    """
    if not os.path.exists(config_path) and config_path == '/app/config.yaml':
        config_path = 'config.yaml'
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)



def run_logparser_llm(input_files, output_dir, use_cache=False, write_cache=False, cache_dir='data/cache', time_limit=None, icl_selection_strategy=None):
    """Executes the LogParser-LLM parsing pipeline.

    Utilizes prefix trees for log routing and queries LLM context
    for unrouted templates, keeping an adaptive template manager.

    Args:
        input_files (list): List of input JSONL file paths.
        output_dir (str): Output directory to write results.
        use_cache (bool): Enable loading parser templates from cache.
        write_cache (bool): Enable saving parser templates to cache.
        cache_dir (str): Path to cache directory.
        time_limit (float, optional): Maximum duration in seconds allowed for parsing.
    """
    tree_router = PrefixTree()
    
    if use_cache:
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, 'logparser_llm_cache.json')
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as cf:
                    cached_templates = json.load(cf)
                    for tmpl in cached_templates:
                        tree_router.insert(tmpl)
                logger.info(f"Loaded {len(cached_templates)} templates from cache.")
            except Exception as e:
                logger.error(f"Error loading cache: {e}")

    llm_extractor = LLMExtractor(tree_router, icl_selection_strategy=icl_selection_strategy)
    template_manager = TemplateManager(tree_router)
    
    os.makedirs(output_dir, exist_ok=True)
    
    for in_file in input_files:
        base_name = os.path.basename(in_file)
        out_file = os.path.join(output_dir, f"parsed_{base_name}")
        parser_name = f"parsed_{base_name.replace('.jsonl', '')}"
        logger.info(f"Processing {in_file} with logparser-llm (round-robin)...")
        
        history = []
        cache_hits = 0
        llm_invocations = 0

        file_start = time.perf_counter()
        last_log_time = file_start
        with open(in_file, 'r', encoding='utf-8') as f_in:
            lines = f_in.readlines()
            
        total_lines = len(lines)
        
        # Group lines by dataset name extracted from event.id
        lines_by_dataset = {}
        for line in lines:
            try:
                record = json.loads(line.strip())
                line_id = record.get('event', {}).get('id') or record.get('LineId') or 'default'
                ds = line_id.split('_')[0] if '_' in line_id else 'default'
                if ds not in lines_by_dataset:
                    lines_by_dataset[ds] = []
                lines_by_dataset[ds].append(line)
            except Exception:
                if 'default' not in lines_by_dataset:
                    lines_by_dataset['default'] = []
                lines_by_dataset['default'].append(line)
                
        # Interleave round-robin in chunks of size 5000
        CHUNK_SIZE = 5000
        datasets_list = list(lines_by_dataset.keys())
        indices = {ds: 0 for ds in datasets_list}
        
        interleaved_lines = []
        any_remaining = True
        while any_remaining:
            any_remaining = False
            for ds in datasets_list:
                start_idx = indices[ds]
                if start_idx < len(lines_by_dataset[ds]):
                    end_idx = min(start_idx + CHUNK_SIZE, len(lines_by_dataset[ds]))
                    for idx in range(start_idx, end_idx):
                        interleaved_lines.append(lines_by_dataset[ds][idx])
                    indices[ds] = end_idx
                    any_remaining = True

        parsed_records = {}
        
        for line_idx, line in enumerate(tqdm(interleaved_lines, desc=f"Parsing {base_name}")):
            current_time = time.perf_counter()
            elapsed = current_time - file_start
            if time_limit and elapsed > time_limit:
                logger.warning(f"Time limit of {format_duration(time_limit)} reached. Stopping early.")
                break
                
            # Periodic progress logging
            if current_time - last_log_time >= 10.0:
                last_log_time = current_time
                pct = (line_idx / total_lines) * 100 if total_lines > 0 else 0
                rate = line_idx / elapsed if elapsed > 0 else 0
                limit_str = f" | Time Left: {format_duration(time_limit - elapsed)}" if time_limit else ""
                logger.info(
                    f"Progress (LogParser-LLM): parsed {line_idx}/{total_lines} ({pct:.2f}%) | "
                    f"Speed: {rate:.1f} logs/s | Cache Hits: {cache_hits} | "
                    f"LLM Calls: {llm_invocations}{limit_str}"
                )
                
            try:
                record = json.loads(line.strip())
                line_id = record.get('event', {}).get('id') or record.get('LineId') or str(line_idx)
                log_message = record.get('message', '')
                if not log_message:
                    record['parsed_template'] = ''
                    parsed_records[line_id] = record
                    continue
                    
                tokens = log_message.split(' ')
                
                template = tree_router.strict_match(tokens)
                if template:
                    cache_hits += 1
                else:
                    template = tree_router.loose_match(tokens)
                    if template:
                        cache_hits += 1
                    else:
                        template = llm_extractor.get_template(log_message, record)
                        llm_invocations += 1
                    
                record['parsed_template'] = template
                parsed_records[line_id] = record
                
                history.append({
                    'log_volume': line_idx + 1,
                    'llm_invocations': llm_invocations,
                    'cache_hits': cache_hits
                })

                if (line_idx + 1) % 1000 == 0:
                    template_manager.calibrate()
                    tree_router.prune_inactive_templates()
                    tree_router.prune_to_capacity(max_templates=1000)
                    
            except Exception as e:
                logger.error(f"Error parsing line in {in_file}: {e}")
                
        template_manager.calibrate()
        tree_router.prune_inactive_templates()
        tree_router.prune_to_capacity(max_templates=1000)
        
        # Write outputs back in the original input order (only writing what was parsed)
        with open(out_file, 'w', encoding='utf-8') as f_out:
            for line_idx, line in enumerate(lines):
                try:
                    record = json.loads(line.strip())
                    line_id = record.get('event', {}).get('id') or record.get('LineId') or str(line_idx)
                    if line_id in parsed_records:
                        f_out.write(json.dumps(parsed_records[line_id]) + '\n')
                except Exception as e:
                    logger.error(f"Error writing line: {e}")

        file_elapsed = time.perf_counter() - file_start
        logger.info(f"LogParser-LLM finished parsing {base_name} in {format_duration(file_elapsed)}.")
        
        usage_stats = llm_extractor.llm_client.get_usage()
        profile_file = os.path.join(output_dir, f"{parser_name}_profile.json")
        model_name = llm_extractor.llm_client.model_name
        if not isinstance(model_name, str):
            model_name = "mock-model"
        else:
            model_name = model_name.replace(':', '-')
        try:
            with open(profile_file, 'w', encoding='utf-8') as pf:
                json.dump({
                    "time_taken_seconds": file_elapsed,
                    "llm_invocations": usage_stats.get("invocations", 0),
                    "prompt_tokens": usage_stats.get("prompt_tokens", 0),
                    "completion_tokens": usage_stats.get("completion_tokens", 0),
                    "total_tokens": usage_stats.get("total_tokens", 0),
                    "llm_timeouts": usage_stats.get("llm_timeouts", 0),
                    "failed_invocations": usage_stats.get("failed_invocations", 0),
                    "cache_hits": cache_hits,
                    "log_volume": total_lines,
                    "history": history,
                    "model_used": model_name,
                    "method_used": "logparser-llm"
                }, pf, indent=4)
        except Exception as e:
            logger.error(f"Error saving profile: {e}")

    if write_cache:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            cache_file = os.path.join(cache_dir, 'logparser_llm_cache.json')
            existing_clusters = []
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as cf:
                        existing_clusters = json.load(cf)
                except Exception as e:
                    logger.error(f"Error loading existing logparser-llm cache for merge: {e}")
            
            merged_clusters = list(existing_clusters)
            seen_templates = set(merged_clusters)
            for c in tree_router.clusters:
                if c not in seen_templates:
                    seen_templates.add(c)
                    merged_clusters.append(c)
                    
            with open(cache_file, 'w', encoding='utf-8') as cf:
                json.dump(merged_clusters, cf, indent=4)
            logger.info(f"Saved {len(merged_clusters)} templates to cache.")
        except Exception as e:
            logger.error(f"Error saving cache: {e}")

def regex_to_standard_template(regex_str: str) -> str:
    """Converts a regex template pattern string back to a standard log template containing <*>."""
    if not isinstance(regex_str, str):
        return ""
    # 1. Replace (.*?) with <*>
    template = regex_str.replace("(.*?)", "<*>")
    # 2. Strip trailing $ anchor
    if template.endswith("$"):
        template = template[:-1]
    # 3. Unescape special regex characters: \. \- \+ \? \* \^ \$ \( \) \[ \] \{ \} \| \\
    special_chars = [
        ("\\.", "."), ("\\-", "-"), ("\\+", "+"), ("\\?", "?"), ("\\*", "*"),
        ("\\^", "^"), ("\\$", "$"), ("\\(", "("), ("\\)", ")"), ("\\[", "["),
        ("\\]", "]"), ("\\{", "{"), ("\\}", "}"), ("\\|", "|"), ("\\\\", "\\")
    ]
    for esc, unesc in special_chars:
        template = template.replace(esc, unesc)
    return template

def main():
    """Main routing controller that reads CLI arguments and invokes the chosen parser."""
    parser = argparse.ArgumentParser(description="Unified Parser")
    parser.add_argument('--method', type=str, required=True, choices=['logparser-llm', 'logbatcher', 'librelog'])
    
    use_cache_env = os.environ.get('USE_CACHE', 'false').lower() == 'true'
    write_cache_env = os.environ.get('WRITE_CACHE', 'false').lower() == 'true'
    llm_debug_env = os.environ.get('LLM_DEBUG', 'false').lower() == 'true'
    
    icl_strategy_env = os.environ.get('ICL_SELECTION_STRATEGY', None)
    
    parser.add_argument('--use-cache', action='store_true', default=use_cache_env, help='Use cached templates from previous runs')
    parser.add_argument('--write-cache', action='store_true', default=write_cache_env, help='Write templates to cache on exit')
    parser.add_argument('--time-limit', type=float, default=None, help='Maximum execution duration in seconds')
    parser.add_argument('--icl-selection-strategy', type=str, default=icl_strategy_env, choices=['similarity', 'diversity'], help='ICL selection strategy')
    parser.add_argument('--llm-debug', action='store_true', default=llm_debug_env, help='Enable raw LLM requests/responses/errors logging to llm_debug.jsonl')
    args = parser.parse_args()
    
    if args.llm_debug:
        os.environ['LLM_DEBUG'] = 'true'
    
    config = load_config()
    directories = config.get('directories', {})
    dataset_name = directories.get('dataset_name', 'loghub')
    
    input_base = directories.get('output_dir', 'data/processed')
    input_dir = os.path.join(input_base, dataset_name)
    
    parsed_dir = os.path.join('data/parsed', dataset_name)
    os.makedirs(parsed_dir, exist_ok=True)
    
    cache_base = directories.get('cache_dir', 'data/cache')
    cache_dir = os.path.join(cache_base, dataset_name)
    
    input_files = glob.glob(os.path.join(input_dir, '*.jsonl'))
    
    if not input_files:
        logger.warning(f"No JSONL files found in {input_dir}. Nothing to parse.")
        return
        
    if args.method == 'logparser-llm':
        run_logparser_llm(input_files, parsed_dir, use_cache=args.use_cache, write_cache=args.write_cache, cache_dir=cache_dir, time_limit=args.time_limit, icl_selection_strategy=args.icl_selection_strategy)
    elif args.method == 'logbatcher':
        import csv
        output_csv = os.path.join(parsed_dir, 'logbatcher_output.csv')

        # Load all logs from all JSONL files in input_dir
        logs_to_parse = []
        for in_file in input_files:
            logger.info(f"Reading logs from {in_file} for LogBatcher...")
            with open(in_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        msg = record.get('message') or record.get('Content') or ""
                        line_id = record.get('event', {}).get('id') or record.get('LineId') or str(len(logs_to_parse) + 1)
                        if msg:
                            logs_to_parse.append({
                                'id': line_id,
                                'message': msg
                            })
                    except Exception as e:
                        logger.error(f"Error reading line: {e}")
                        
        if not logs_to_parse:
            logger.warning("No logs loaded for LogBatcher.")
            return
            
        logger.info(f"Instantiating LogBatcher and parsing {len(logs_to_parse)} logs (round-robin)...")
        from core.logbatcher.parser import LogBatcher

        parser_instance = LogBatcher()

        if args.use_cache:
            os.makedirs(cache_dir, exist_ok=True)
            cache_file = os.path.join(cache_dir, 'logbatcher_cache.json')
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as cf:
                        parser_instance.cache.cache = json.load(cf)
                    logger.info(f"Loaded {len(parser_instance.cache.cache)} cache entries for LogBatcher.")
                except Exception as e:
                    logger.error(f"Error loading LogBatcher cache: {e}")

        start_time = time.perf_counter()

        # Group logs by dataset, then round-robin interleave in chunks so no single
        # dataset monopolizes the buffer if --time-limit cuts the run short.
        logs_by_dataset = {}
        for log in logs_to_parse:
            ds = log['id'].split('_')[0] if '_' in log['id'] else 'default'
            if ds not in logs_by_dataset:
                logs_by_dataset[ds] = []
            logs_by_dataset[ds].append(log)

        CHUNK_SIZE = 5000
        datasets_list = list(logs_by_dataset.keys())
        indices = {ds: 0 for ds in datasets_list}
        interleaved_logs = []
        any_remaining = True
        while any_remaining:
            any_remaining = False
            for ds in datasets_list:
                start_idx = indices[ds]
                if start_idx < len(logs_by_dataset[ds]):
                    end_idx = min(start_idx + CHUNK_SIZE, len(logs_by_dataset[ds]))
                    interleaved_logs.extend(logs_by_dataset[ds][start_idx:end_idx])
                    indices[ds] = end_idx
                    any_remaining = True

        parsed = parser_instance.parse(interleaved_logs, time_limit=args.time_limit, start_time=start_time)
        results_by_id = {res['id']: res['template'] for res in parsed}

        # Output list matching original logs_to_parse order (only writing parsed logs)
        results_list = []
        for log in logs_to_parse:
            if log['id'] in results_by_id:
                results_list.append({
                    'id': log['id'],
                    'message': log['message'],
                    'template': results_by_id[log['id']]
                })

        elapsed = time.perf_counter() - start_time
        logger.info(f"LogBatcher finished parsing in {format_duration(elapsed)}.")
        
        usage_stats = parser_instance.llm_client.get_usage()
        profile_file = os.path.join(parsed_dir, 'logbatcher_profile.json')
        model_name = parser_instance.llm_client.model_name
        if not isinstance(model_name, str):
            model_name = "mock-model"
        else:
            model_name = model_name.replace(':', '-')
        batcher_history = getattr(parser_instance, 'history', [])
        cache_hits = batcher_history[-1].get('cache_hits', 0) if batcher_history else 0
        try:
            with open(profile_file, 'w', encoding='utf-8') as pf:
                json.dump({
                    "time_taken_seconds": elapsed,
                    "llm_invocations": usage_stats.get("invocations", 0),
                    "prompt_tokens": usage_stats.get("prompt_tokens", 0),
                    "completion_tokens": usage_stats.get("completion_tokens", 0),
                    "total_tokens": usage_stats.get("total_tokens", 0),
                    "llm_timeouts": usage_stats.get("llm_timeouts", 0),
                    "failed_invocations": usage_stats.get("failed_invocations", 0),
                    "cache_hits": cache_hits,
                    "log_volume": len(logs_to_parse),
                    "history": batcher_history,
                    "model_used": model_name,
                    "method_used": "logbatcher"
                }, pf, indent=4)
        except Exception as e:
            logger.error(f"Error saving LogBatcher profile: {e}")
        
        if args.write_cache:
            try:
                cache_file = os.path.join(cache_dir, 'logbatcher_cache.json')
                existing_entries = []
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'r', encoding='utf-8') as cf:
                            existing_entries = json.load(cf)
                    except Exception as e:
                        logger.error(f"Error loading existing LogBatcher cache for merge: {e}")
                
                merged_entries = list(existing_entries)
                seen_templates = {entry['template'] for entry in merged_entries if 'template' in entry}
                for entry in parser_instance.cache.cache:
                    if entry.get('template') not in seen_templates:
                        seen_templates.add(entry['template'])
                        merged_entries.append({
                            'template': entry['template'],
                            'ref_log': entry['ref_log'],
                            'frequency': entry['frequency']
                        })
                
                with open(cache_file, 'w', encoding='utf-8') as cf:
                    json.dump(merged_entries, cf, indent=4)
                logger.info(f"Saved {len(merged_entries)} cache entries to cache.")
            except Exception as e:
                logger.error(f"Error saving LogBatcher cache: {e}")
        
        logger.info(f"Saving LogBatcher output to {output_csv}...")
        with open(output_csv, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['LineId', 'Content', 'EventTemplate'])
            for res in results_list:
                writer.writerow([res['id'], res['message'], res['template']])
        logger.info("LogBatcher pipeline finished successfully.")
    elif args.method == 'librelog':
        import csv
        output_csv = os.path.join(parsed_dir, 'librelog_output.csv')
        from core.librelog.parser import LibreLogParser
        
        # Load all logs from all JSONL files in input_dir
        logs_to_parse = []
        for in_file in input_files:
            logger.info(f"Reading logs from {in_file} for LibreLog...")
            with open(in_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        msg = record.get('message') or record.get('Content') or ""
                        line_id = record.get('event', {}).get('id') or record.get('LineId') or str(len(logs_to_parse) + 1)
                        if msg:
                            logs_to_parse.append({
                                'id': line_id,
                                'message': msg
                            })
                    except Exception as e:
                        logger.error(f"Error reading line: {e}")
                        
        if not logs_to_parse:
            logger.warning("No logs loaded for LibreLog.")
            return
            
        logger.info(f"Setting up LibreLog round-robin execution for {len(logs_to_parse)} logs...")
        memory_list = []
        
        if args.use_cache:
            os.makedirs(cache_dir, exist_ok=True)
            cache_file = os.path.join(cache_dir, 'librelog_cache.json')
            if os.path.exists(cache_file):
                try:
                    with open(cache_file, 'r', encoding='utf-8') as cf:
                        cached_mem = json.load(cf)
                        for entry in cached_mem:
                            gk_list = entry['group_key']
                            gk_tuple = (gk_list[0], tuple(gk_list[1]))
                            memory_list.append({
                                'raw_log': entry['raw_log'],
                                'template': entry['template'],
                                'group_key': gk_tuple
                            })
                    logger.info(f"Loaded {len(memory_list)} memory entries for initialization.")
                except Exception as e:
                    logger.error(f"Error loading LibreLog cache: {e}")
                    
        # Group logs by dataset
        logs_by_dataset = {}
        for log in logs_to_parse:
            ds = log['id'].split('_')[0] if '_' in log['id'] else 'default'
            if ds not in logs_by_dataset:
                logs_by_dataset[ds] = []
            logs_by_dataset[ds].append(log)
            
        # Instantiate dedicated parsers per dataset to isolate caches and prevent thrashing
        dataset_parsers = {}
        for ds in logs_by_dataset.keys():
            parser_inst = LibreLogParser(dataset_name=ds)
            if memory_list:
                parser_inst.memory.memory = list(memory_list)
            dataset_parsers[ds] = parser_inst

        start_time = time.perf_counter()
        
        parsed_results = {}
        log_volume = 0
        cache_hits = 0
        llm_invocations = 0
        total_logs = len(logs_to_parse)
        
        for ds, parser_inst in dataset_parsers.items():
            current_time = time.perf_counter()
            elapsed = current_time - start_time
            if args.time_limit and elapsed > args.time_limit:
                logger.warning(f"Time limit of {format_duration(args.time_limit)} reached. Stopping early.")
                break
                
            dataset_logs = logs_by_dataset[ds]
            logger.info(f"Parsing dataset: {ds} ({len(dataset_logs)} logs)")
            
            try:
                results = parser_inst.parse(
                    dataset_logs,
                    time_limit=args.time_limit - elapsed if args.time_limit else None,
                    start_time=start_time
                )
                for res in results:
                    parsed_results[res['id']] = res['template']
                
                if parser_inst.history:
                    last_hist = parser_inst.history[-1]
                    log_volume += last_hist['log_volume']
                    cache_hits += last_hist['cache_hits']
            except Exception as e:
                logger.error(f"Error parsing dataset {ds}: {e}")
            
        elapsed = time.perf_counter() - start_time
        logger.info(f"LibreLog finished parsing in {format_duration(elapsed)}.")
        
        total_invocations = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens_count = 0
        total_llm_timeouts = 0
        total_failed_invocations = 0
        
        for ds, parser_inst in dataset_parsers.items():
            usage_stats = parser_inst.llm_client.get_usage()
            total_invocations += usage_stats.get("invocations", 0)
            total_prompt_tokens += usage_stats.get("prompt_tokens", 0)
            total_completion_tokens += usage_stats.get("completion_tokens", 0)
            total_tokens_count += usage_stats.get("total_tokens", 0)
            total_llm_timeouts += usage_stats.get("llm_timeouts", 0)
            total_failed_invocations += usage_stats.get("failed_invocations", 0)
            model_name = parser_inst.llm_client.model_name
            
        profile_file = os.path.join(parsed_dir, 'librelog_profile.json')
        if not isinstance(model_name, str):
            model_name = "mock-model"
        else:
            model_name = model_name.replace(':', '-')
        try:
            with open(profile_file, 'w', encoding='utf-8') as pf:
                json.dump({
                    "time_taken_seconds": elapsed,
                    "llm_invocations": total_invocations,
                    "prompt_tokens": total_prompt_tokens,
                    "completion_tokens": total_completion_tokens,
                    "total_tokens": total_tokens_count,
                    "llm_timeouts": total_llm_timeouts,
                    "failed_invocations": total_failed_invocations,
                    "cache_hits": cache_hits,
                    "log_volume": total_logs,
                    "history": [],
                    "model_used": model_name,
                    "method_used": "librelog"
                }, pf, indent=4)
        except Exception as e:
            logger.error(f"Error saving LibreLog profile: {e}")
        
        if args.write_cache:
            try:
                os.makedirs(cache_dir, exist_ok=True)
                cache_file = os.path.join(cache_dir, 'librelog_cache.json')
                existing_entries = []
                if os.path.exists(cache_file):
                    try:
                        with open(cache_file, 'r', encoding='utf-8') as cf:
                            existing_entries = json.load(cf)
                    except Exception as e:
                        logger.error(f"Error loading existing librelog cache for merge: {e}")
                        
                serializable_mem = list(existing_entries)
                seen_keys = {(entry['raw_log'], entry['template']) for entry in serializable_mem}
                
                for ds, parser_inst in dataset_parsers.items():
                    for entry in parser_inst.memory.memory:
                        key = (entry['raw_log'], entry['template'])
                        if key not in seen_keys:
                            seen_keys.add(key)
                            serializable_mem.append({
                                'raw_log': entry['raw_log'],
                                'template': entry['template'],
                                'group_key': [entry['group_key'][0], list(entry['group_key'][1])]
                            })
                with open(cache_file, 'w', encoding='utf-8') as cf:
                    json.dump(serializable_mem, cf, indent=4)
                logger.info(f"Saved {len(serializable_mem)} unique memory entries to cache.")
            except Exception as e:
                logger.error(f"Error saving LibreLog cache: {e}")
        
        logger.info(f"Saving LibreLog output to {output_csv}...")
        with open(output_csv, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(['LineId', 'Content', 'EventTemplate'])
            for log in logs_to_parse:
                if log['id'] in parsed_results:
                    writer.writerow([log['id'], log['message'], regex_to_standard_template(parsed_results[log['id']])])
        logger.info("LibreLog pipeline finished successfully.")

if __name__ == "__main__":
    main()
