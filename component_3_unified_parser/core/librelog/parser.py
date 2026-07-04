import yaml
from core.llm_client import OllamaClient
from .regex_manager import RegexManager
from .grouping import GroupingManager
from .memory import LogMemory
from .llama_parser import LlamaParser

class LibreLogParser:
    def __init__(self, config_path='/app/config.yaml'):
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

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
        masked_log = self.regex_manager.mask(raw_log)

        group_key = self.grouping_manager.get_group_key(masked_log)
        
        exact_template = self.memory.get_exact_match(masked_log, group_key)
        if exact_template:
            return exact_template

        examples = self.memory.get_similar_logs(masked_log, self.k_shots, group_key)

        final_template = self.llama_parser.parse_log(masked_log, examples)

        self.memory.add(masked_log, final_template, group_key)

        return final_template

    def parse(self, logs_to_parse):
        results = []
        for log in logs_to_parse:
            raw_log = log.get('message', '')
            template = self.parse_single_log(raw_log)
            results.append({
                'id': log['id'],
                'message': raw_log,
                'template': template
            })
        return results
