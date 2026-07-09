"""LibreLog pipeline orchestrator.

Implements regex preprocessing, semantic clustering, dynamic example memory lookup,
and LLM-based template parsing with self-reflection.
"""

import yaml
from core.llm_client import OllamaClient
from .regex_manager import RegexManager
from .grouping import GroupingManager
from .memory import LogMemory
from .llama_parser import LlamaParser

class LibreLogParser:
    """LibreLog hybrid template parsing system using regex, memory lookup, and LLMs."""

    def __init__(self, config_path='/app/config.yaml'):
        """Initializes the LibreLogParser.

        Args:
            config_path (str): YAML file config path. Defaults to '/app/config.yaml'.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}

        ll_config = config.get('librelog', {})
        self.similarity_threshold = ll_config.get('similarity_threshold', 0.85)
        self.max_memory_size = ll_config.get('max_memory_size', 1000)
        self.k_shots = ll_config.get('k_shots', 3)
        self.enable_reflection = ll_config.get('enable_reflection', True)

        self.llm_client = OllamaClient(config_path)

        self.regex_manager = RegexManager()
        self.grouping_manager = GroupingManager()
        self.memory = LogMemory(max_size=self.max_memory_size, similarity_threshold=self.similarity_threshold)
        self.llama_parser = LlamaParser(self.llm_client, enable_reflection=self.enable_reflection)

    def parse_single_log(self, raw_log):
        """Filters, cleans, and parses a single log line to extract its template.

        Masks variables using static regex rules, checks the internal memory
        cache, and runs a few-shot LLM reflection prompt if unmapped.

        Args:
            raw_log (str): Raw log message content.

        Returns:
            str: Evaluated static template.
        """
        masked_log = self.regex_manager.mask(raw_log)

        group_key = self.grouping_manager.get_group_key(masked_log)
        
        exact_template = self.memory.get_exact_match(masked_log, group_key)
        if exact_template:
            return exact_template

        examples = self.memory.get_similar_logs(masked_log, self.k_shots, group_key)

        final_template = self.llama_parser.parse_log(masked_log, examples)

        self.memory.add(masked_log, final_template, group_key)

        return final_template

    def parse(self, logs_to_parse, time_limit=None, start_time=None):
        """Parses list of logs using LibreLog's pipeline logic.

        Args:
            logs_to_parse (list): List of log dictionaries containing message and ID.
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: List of parsed log dictionaries with predicted templates.
        """
        import time
        if start_time is None:
            start_time = time.perf_counter()

        results = []
        history = []
        cache_hits = 0
        llm_invocations = 0
        
        total_logs = len(logs_to_parse)
        last_log_time = start_time
        import logging
        ll_logger = logging.getLogger(__name__)

        for idx, log in enumerate(logs_to_parse):
            current_time = time.perf_counter()
            elapsed = current_time - start_time
            if time_limit and elapsed > time_limit:
                ll_logger.warning(f"Time limit of {time_limit} seconds reached. Stopping early.")
                break

            # Periodic status logging
            if current_time - last_log_time >= 10.0:
                last_log_time = current_time
                pct = (idx / total_logs) * 100 if total_logs > 0 else 0
                rate = idx / elapsed if elapsed > 0 else 0
                limit_str = f" | Time Left: {time_limit - elapsed:.1f}s" if time_limit else ""
                ll_logger.info(
                    f"Progress (LibreLog): parsed {idx}/{total_logs} ({pct:.2f}%) | "
                    f"Speed: {rate:.1f} logs/s | Cache Hits: {cache_hits} | "
                    f"LLM Calls: {llm_invocations}{limit_str}"
                )

            raw_log = log.get('message', '')
            
            masked_log = self.regex_manager.mask(raw_log)
            group_key = self.grouping_manager.get_group_key(masked_log)
            exact_template = self.memory.get_exact_match(masked_log, group_key)
            
            if exact_template:
                template = exact_template
                cache_hits += 1
            else:
                examples = self.memory.get_similar_logs(masked_log, self.k_shots, group_key)
                template = self.llama_parser.parse_log(masked_log, examples)
                self.memory.add(masked_log, template, group_key)
                llm_invocations += 1

            results.append({
                'id': log['id'],
                'message': raw_log,
                'template': template
            })
            history.append({
                'log_volume': idx + 1,
                'llm_invocations': llm_invocations,
                'cache_hits': cache_hits
            })

        self.history = history
        return results
