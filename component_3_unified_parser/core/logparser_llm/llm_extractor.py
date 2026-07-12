"""In-Context Learning (ICL) Template Extractor for LogParser-LLM.

Computes log embeddings, extracts similar examples dynamically from a seed pool,
and queries the Ollama client to extract templates with semantic categories.
"""

import re
import json
import os
import logging
import yaml
from core.llm_client import OllamaClient

logger = logging.getLogger(__name__)

def get_jaccard_similarity(str1, str2):
    """Computes Jaccard Similarity between two strings on a token level."""
    tokens1 = set(str1.split())
    tokens2 = set(str2.split())
    if not tokens1 and not tokens2:
        return 1.0
    union_len = len(tokens1.union(tokens2))
    if union_len == 0:
        return 0.0
    return len(tokens1.intersection(tokens2)) / union_len

def get_variables_from_example(template, ref_log):
    """Automatically extracts dynamic variables matching tags in the template from ref_log."""
    tags = re.findall(r'<[A-Z]{3}>', template)
    if not tags:
        return []

    escaped_template = re.escape(template)
    pattern_str = escaped_template
    for tag in set(tags):
        pattern_str = pattern_str.replace(re.escape(tag), r'(.*?)')

    try:
        pattern = re.compile(f"^{pattern_str}$")
        match = pattern.match(ref_log)
        if match:
            variables = []
            for idx, val in enumerate(match.groups()):
                if idx < len(tags):
                    variables.append({
                        "category": tags[idx],
                        "value": val.strip()
                    })
            return variables
    except Exception:
        pass
    return []

class LLMExtractor:
    """Handles parsing logs using dynamic few-shot templates querying the LLM."""

    def __init__(self, tree_router, config_path='/app/config.yaml', icl_selection_strategy=None):
        """Initializes LLMExtractor.

        Args:
            tree_router (PrefixTree): In-memory PrefixTree router references.
            config_path (str): YAML configuration path. Defaults to '/app/config.yaml'.
            icl_selection_strategy (str, optional): Overrides YAML selection strategy.
        """
        self.tree_router = tree_router
        self.llm_client = OllamaClient(config_path)
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}
        self.k_shots = config.get('logparser_llm', {}).get('k_shots', 3)
        cat_val = config.get('logparser_llm', {}).get('categories_mode', 'ecs_10')
        if str(cat_val) == "10":
            self.categories_mode = "ecs_10"
        elif str(cat_val) == "3":
            self.categories_mode = "ecs_3"
        else:
            self.categories_mode = str(cat_val)
        
        if icl_selection_strategy is not None:
            self.icl_selection_strategy = icl_selection_strategy
        else:
            self.icl_selection_strategy = config.get('logparser_llm', {}).get('icl_selection_strategy', 'similarity')
        self.template_pool = []
        
        # Load Template Pool from the existing LogBatcher cache file if available
        cache_file = '/app/data/cache/logbatcher_cache.json'
        if not os.path.exists(cache_file):
            cache_file = 'data/cache/logbatcher_cache.json'
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
                    for entry in entries:
                        if 'template' in entry and 'ref_log' in entry:
                            self.template_pool.append({
                                'template': entry['template'],
                                'ref_log': entry['ref_log']
                            })
            except Exception as e:
                logger.error(f"Error loading template pool for ICL: {e}")

        # Load Human-in-the-loop Calibration Seed
        calibration_file = config.get('logparser_llm', {}).get('calibration_file', '/app/data/cache/calibration_seed.json')
        if not os.path.exists(calibration_file) and calibration_file.startswith('/app/'):
            calibration_file = calibration_file.replace('/app/', '')
        if os.path.exists(calibration_file):
            try:
                with open(calibration_file, 'r', encoding='utf-8') as f:
                    calib_entries = json.load(f)
                    for entry in calib_entries:
                        if 'template' in entry and 'ref_log' in entry:
                            # Prepend to pool so calibration takes precedence if deduplicated later
                            self.template_pool.insert(0, {
                                'template': entry['template'],
                                'ref_log': entry['ref_log']
                            })
                            # Also insert into tree router so it can be strict/loose matched immediately
                            self.tree_router.insert(entry['template'])
            except Exception as e:
                logger.error(f"Error loading calibration seed: {e}")

        # Fallback seed examples if template pool is empty to ensure always functioning shot retrieval
        if not self.template_pool:
            if self.categories_mode == "ecs_10":
                self.template_pool = [
                    {"template": "User <USR> logged in from <LOI>", "ref_log": "User admin logged in from 1.2.3.4"},
                    {"template": "mice: PS/2 mouse device version <VER>", "ref_log": "mice: PS/2 mouse device version v1.2.3"},
                    {"template": "bindcache: failed init IPS on port <POR>: <STA> (<OID>)", "ref_log": "bindcache: failed init IPS on port 8080: failure (Out of memory)"}
                ]
            elif self.categories_mode == "paper_10":
                self.template_pool = [
                    {"template": "User <OID> logged in from <LOI>", "ref_log": "User admin logged in from 1.2.3.4"},
                    {"template": "mice: PS/2 mouse device version <OTP>", "ref_log": "mice: PS/2 mouse device version v1.2.3"},
                    {"template": "bindcache: failed init IPS on port <CRS>: <STC> (<OTP>)", "ref_log": "bindcache: failed init IPS on port 8080: failure (Out of memory)"}
                ]
            else:
                self.template_pool = [
                    {"template": "User <LOI> logged in", "ref_log": "User admin logged in"},
                    {"template": "mice: PS/2 mouse device common for all mice", "ref_log": "mice: PS/2 mouse device common for all mice"},
                    {"template": "bindcache: failed init IPS: <TDA> (<OID>)", "ref_log": "bindcache: failed init IPS: 0x5 (Out of memory)"}
                ]

    def get_template(self, log_message, log_record=None):
        """Retrieves template and extracts ECS fields using dynamic Jaccard-based K-shot prompting.

        Args:
            log_message (str): Raw log message content.
            log_record (dict, optional): Reference log record dict to enrich with ECS fields.

        Returns:
            str: Evaluated static template.
        """
        if self.icl_selection_strategy == "diversity":
            import random
            if len(self.template_pool) <= self.k_shots:
                top_k_entries = self.template_pool
            else:
                top_k_entries = []
                remaining = self.template_pool.copy()
                first = random.choice(remaining)
                top_k_entries.append(first)
                remaining.remove(first)
                while len(top_k_entries) < self.k_shots and remaining:
                    best_entry = None
                    best_max_sim = float('inf')
                    for entry in remaining:
                        max_sim = max([get_jaccard_similarity(entry['ref_log'], sel['ref_log']) for sel in top_k_entries])
                        if max_sim < best_max_sim:
                            best_max_sim = max_sim
                            best_entry = entry
                    top_k_entries.append(best_entry)
                    remaining.remove(best_entry)
            top_k = [(1.0, e['ref_log'], e['template']) for e in top_k_entries]
        else:
            candidates = []
            for entry in self.template_pool:
                sim = get_jaccard_similarity(log_message, entry['ref_log'])
                candidates.append((sim, entry['ref_log'], entry['template']))
            candidates.sort(key=lambda x: x[0], reverse=True)
            top_k = candidates[:self.k_shots]

        demonstrations = ""
        for sim, ref_log, template in top_k:
            variables = get_variables_from_example(template, ref_log)
            ex_json = {
                "template": template,
                "variables": variables
            }
            demonstrations += f"Log: {ref_log}\nOutput: {json.dumps(ex_json)}\n\n"

        if self.categories_mode == "ecs_10":
            sys_prompt = (
                "As a log parser, your task is to analyze logs and identify dynamic variables. "
                "The allowed semantic categories are:\n"
                "1. <TDA>: Time, date, or activity events\n"
                "2. <LOI>: Location Indicator (e.g. IP address, hostname, URI)\n"
                "3. <OID>: Object Identifier (e.g. Filepath, UUID, hash, session ID)\n"
                "4. <USR>: User Information (e.g. username, email, account ID)\n"
                "5. <POR>: Port number\n"
                "6. <STA>: Status codes, execution outcomes, or states\n"
                "7. <VER>: Version info (software version, OS release)\n"
                "8. <PRO>: Network protocol (e.g. TCP, UDP, HTTP)\n"
                "9. <NUM>: General numeric value\n"
                "10. <COM>: System command, component name, or process label\n\n"
                "You MUST output a valid JSON object containing the normalized template string "
                "where variables are replaced by their category tokens, and a list of extracted variables.\n"
                "CRITICAL: Do NOT include any markdown code blocks, introductory text, conversational preamble, or explanation. Output ONLY the raw JSON object."
            )
            ECS_MAPPING = {
                "<TDA>": "event.ingested",
                "<LOI>": "source.ip",
                "<OID>": "file.path",
                "<USR>": "user.name",
                "<POR>": "source.port",
                "<STA>": "event.outcome",
                "<VER>": "service.version",
                "<PRO>": "network.transport",
                "<NUM>": "event.duration",
                "<COM>": "process.name"
            }
        elif self.categories_mode == "paper_10":
            sys_prompt = (
                "As a log parser, your task is to analyze logs and identify dynamic variables. "
                "The allowed semantic categories are:\n"
                "1. <OID>: Object ID (e.g. block ID, user ID)\n"
                "2. <LOI>: Location Indicator (e.g. IP address, hostname, node)\n"
                "3. <OBN>: Object Name (e.g. file name, process name)\n"
                "4. <TID>: Type Indicator (e.g. task type, operation type)\n"
                "5. <SID>: Switch Indicator (e.g. flag, boolean state)\n"
                "6. <TDA>: Time/Duration (e.g. timestamps, execution time)\n"
                "7. <CRS>: Computing Resources (e.g. CPU, memory, port)\n"
                "8. <OBA>: Object Amount (e.g. size, length, count)\n"
                "9. <STC>: Status Code (e.g. error code, state identifier)\n"
                "10. <OTP>: Other Parameters (e.g. miscellaneous values)\n\n"
                "You MUST output a valid JSON object containing the normalized template string "
                "where variables are replaced by their category tokens, and a list of extracted variables.\n"
                "CRITICAL: Do NOT include any markdown code blocks, introductory text, conversational preamble, or explanation. Output ONLY the raw JSON object."
            )
            ECS_MAPPING = {
                "<OID>": "file.path",
                "<LOI>": "source.ip",
                "<OBN>": "process.name",
                "<TID>": "event.type",
                "<SID>": "event.action",
                "<TDA>": "event.ingested",
                "<CRS>": "host.cpu",
                "<OBA>": "event.duration",
                "<STC>": "event.outcome",
                "<OTP>": "message"
            }
        else:
            sys_prompt = (
                "As a log parser, your task is to analyze logs and identify dynamic variables. "
                "The allowed semantic categories are: Location Indicator (<LOI>), Object ID (<OID>), and Time/Date/Activity (<TDA>). "
                "You MUST output a valid JSON object containing the normalized template string "
                "where variables are replaced by their category tokens, and a list of extracted variables.\n"
                "CRITICAL: Do NOT include any markdown code blocks, introductory text, conversational preamble, or explanation. Output ONLY the raw JSON object."
            )
            ECS_MAPPING = {
                "<LOI>": "source.ip",
                "<OID>": "file.path",
                "<TDA>": "event.ingested"
            }

        if demonstrations:
            sys_prompt += f"\n\nExamples:\n{demonstrations}"

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Log: {log_message}\nOutput:"}
        ]

        try:
            response = self.llm_client.generate_completion(messages).strip()
            # Clean markdown code blocks if the LLM output wrapped them
            if response.startswith("```"):
                lines = response.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                response = "\n".join(lines).strip()

            try:
                parsed_json = json.loads(response)
                if isinstance(parsed_json, dict):
                    template = parsed_json.get("template", log_message)

                    # Map categories to ECS fields directly on the log record object
                    if log_record is not None and "variables" in parsed_json and isinstance(parsed_json["variables"], list):
                        for var in parsed_json["variables"]:
                            if isinstance(var, dict):
                                cat = var.get("category")
                                val = var.get("value")
                                if cat in ECS_MAPPING and val:
                                    ecs_field = ECS_MAPPING[cat]
                                    log_record[ecs_field] = val
                else:
                    template = response
            except Exception as json_err:
                logger.info(f"LLM output was not valid JSON ({json_err}). Treating raw output as template string.")
                template = response

            # Update PrefixTree Router
            self.tree_router.insert(template)
            # Add to template pool
            self.template_pool.append({
                'template': template,
                'ref_log': log_message
            })
            return template

        except Exception as e:
            logger.error(f"LLM generation/fallback error: {e}. Falling back to literal.")
            self.tree_router.insert(log_message)
            return log_message
