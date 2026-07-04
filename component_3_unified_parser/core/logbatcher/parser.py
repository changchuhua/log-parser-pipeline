"""LogBatcher pipeline orchestrator.

Implements zero-shot diverse parsing routing using partition clusterers,
diversity samplers (DPP), and post-process match and prune logic.
"""

import yaml
from core.llm_client import OllamaClient
from .cluster import get_clusterer
from .parsing_cache import ParsingCache
from .matching import match_log
from .sample import get_sampler
from .parsing_base import ParsingBase
from .postprocess import match_and_prune

class LogBatcher:
    """Zero-shot diverse log parser using mathematical sampling and caching."""

    def __init__(self, config_path='/app/config.yaml'):
        """Initializes the LogBatcher parser.

        Args:
            config_path (str): YAML file config path. Defaults to '/app/config.yaml'.
        """
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)

        lb_config = config.get('logbatcher', {})
        self.batch_size = lb_config.get('batch_size', 10)
        self.cluster_type = lb_config.get('cluster', 'LengthCluster')
        self.sampler_type = lb_config.get('sampler', 'DPPSampler')
        self.similarity_threshold = lb_config.get('cluster_similarity_threshold', 0.8)

        self.llm_client = OllamaClient(config_path)
        self.llm_client.embedding_model = lb_config.get('embedding_model', 'nomic-embed-text')

        self.cache = ParsingCache()
        self.sampler = get_sampler(self.sampler_type, self.llm_client, self.batch_size)
        self.parsing_base = ParsingBase(self.llm_client)

    def parse(self, log_list):
        """Parses a list of logs using clustering, DPP sampling, and Match & Prune.

        Args:
            log_list (list): List of dicts representing logs (must contain 'id' and 'message').

        Returns:
            list: List of parsed log dictionaries containing mapped templates.
        """
        clusterer = get_clusterer(self.cluster_type, log_list, self.similarity_threshold)
        partitions = clusterer.get_partitions()

        parsed_results = {}
        queue = list(partitions)

        while queue:
            partition = queue.pop(0)
            if not partition:
                continue

            first_log = partition[0]
            cached_template = match_log(self.cache, first_log['message'])

            if cached_template:
                matched, pruned = match_and_prune(cached_template, partition, self.cache)
                for log in matched:
                    parsed_results[log['id']] = cached_template
                if pruned:
                    queue.append(pruned)
            else:
                sampled = self.sampler.sample(partition)
                generated_template = self.parsing_base.batch_query(sampled)
                if generated_template:
                    matched, pruned = match_and_prune(generated_template, partition, self.cache)
                    for log in matched:
                        parsed_results[log['id']] = generated_template
                    if pruned:
                        queue.append(pruned)
                else:
                    for log in partition:
                        parsed_results[log['id']] = log['message']

        results = []
        for log in log_list:
            results.append({
                'id': log['id'],
                'message': log['message'],
                'template': parsed_results.get(log['id'], log['message'])
            })
        return results
