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
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}

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

    def parse(self, log_list, time_limit=None, start_time=None):
        """Parses a list of logs using clustering, DPP sampling, and Match & Prune.

        Args:
            log_list (list): List of dicts representing logs (must contain 'id' and 'message').
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: List of parsed log dictionaries containing mapped templates.
        """
        import time
        if start_time is None:
            start_time = time.perf_counter()

        clusterer = get_clusterer(self.cluster_type, log_list, self.similarity_threshold)
        partitions = clusterer.get_partitions()

        parsed_results = {}
        queue = list(partitions)

        history = []
        log_volume = 0
        llm_invocations = 0
        cache_hits = 0
        
        total_logs = len(log_list)
        last_log_time = start_time

        import logging
        lb_logger = logging.getLogger(__name__)

        while queue:
            current_time = time.perf_counter()
            elapsed = current_time - start_time
            if time_limit and elapsed > time_limit:
                lb_logger.warning(f"Time limit of {time_limit} seconds reached. Stopping early.")
                break

            # Periodic status logging
            if current_time - last_log_time >= 10.0:
                last_log_time = current_time
                pct = (log_volume / total_logs) * 100 if total_logs > 0 else 0
                rate = log_volume / elapsed if elapsed > 0 else 0
                limit_str = f" | Time Left: {time_limit - elapsed:.1f}s" if time_limit else ""
                lb_logger.info(
                    f"Progress (LogBatcher): parsed {log_volume}/{total_logs} ({pct:.2f}%) | "
                    f"Speed: {rate:.1f} logs/s | Cache Hits: {cache_hits} | "
                    f"LLM Calls: {llm_invocations}{limit_str}"
                )

            partition = queue.pop(0)
            if not partition:
                continue

            first_log = partition[0]
            cached_template = match_log(self.cache, first_log['message'])

            if cached_template:
                matched, pruned = match_and_prune(cached_template, partition, self.cache)
                for log in matched:
                    parsed_results[log['id']] = cached_template
                
                log_volume += len(matched)
                cache_hits += len(matched)
                history.append({
                    'log_volume': log_volume,
                    'llm_invocations': llm_invocations,
                    'cache_hits': cache_hits
                })
                
                if pruned:
                    queue.append(pruned)
            else:
                sampled = self.sampler.sample(partition, time_limit=time_limit, start_time=start_time)
                generated_template = self.parsing_base.batch_query(sampled)
                if generated_template:
                    matched, pruned = match_and_prune(generated_template, partition, self.cache)
                    if len(matched) > 0:
                        for log in matched:
                            parsed_results[log['id']] = generated_template
                        
                        log_volume += len(matched)
                        llm_invocations += 1
                        history.append({
                            'log_volume': log_volume,
                            'llm_invocations': llm_invocations,
                            'cache_hits': cache_hits
                        })
                        
                        if pruned:
                            queue.append(pruned)
                    else:
                        lb_logger.warning("Generated template failed to match any logs in partition. Falling back to raw messages.")
                        for log in partition:
                            parsed_results[log['id']] = log['message']
                        
                        log_volume += len(partition)
                        llm_invocations += 1
                        history.append({
                            'log_volume': log_volume,
                            'llm_invocations': llm_invocations,
                            'cache_hits': cache_hits
                        })
                else:
                    for log in partition:
                        parsed_results[log['id']] = log['message']
                    
                    log_volume += len(partition)
                    history.append({
                        'log_volume': log_volume,
                        'llm_invocations': llm_invocations,
                        'cache_hits': cache_hits
                    })

        self.history = history

        results = []
        for log in log_list:
            if log['id'] in parsed_results:
                results.append({
                    'id': log['id'],
                    'message': log['message'],
                    'template': parsed_results[log['id']]
                })
        return results
