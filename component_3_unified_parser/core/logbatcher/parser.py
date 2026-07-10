"""LogBatcher pipeline orchestrator.

Implements zero-shot diverse parsing routing using partition clusterers,
diversity samplers (DPP), and post-process match and prune logic.
"""

import time
import yaml
import json
import os
import logging
import collections
from core.llm_client import OllamaClient
from .cluster import get_clusterer
from .parsing_cache import ParsingCache
from .matching import match_log, template_to_regex
from .sample import get_sampler
from .parsing_base import ParsingBase
from .postprocess import match_and_prune

logger = logging.getLogger(__name__)

def jaccard_similarity(tokens1, tokens2):
    """Calculates Jaccard similarity between two token lists."""
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2:
        return 1.0
    union_len = len(set1.union(set2))
    if union_len == 0:
        return 0.0
    return len(set1.intersection(set2)) / union_len

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
        self.vectorizer_type = lb_config.get('vectorizer', 'binary')
        self.use_dynamic_eps = lb_config.get('use_dynamic_eps', False)

        # Buffer parameters
        self.buffer_max_size = lb_config.get('buffer_max_size', 500)
        self.flush_timeout = lb_config.get('flush_timeout_seconds', 5.0)

        self.llm_client = OllamaClient(config_path)
        self.llm_client.embedding_model = lb_config.get('embedding_model', 'nomic-embed-text')

        # Limit cache to 5000 entries
        self.cache = ParsingCache(max_size=5000)
        self.sampler = get_sampler(self.sampler_type, self.llm_client, self.batch_size)
        self.parsing_base = ParsingBase(self.llm_client)

    def parse(self, log_list, time_limit=None, start_time=None):
        """Parses a list of logs using a hybrid buffer DBSCAN clustering architecture.

        Args:
            log_list (list): List of dicts representing logs (must contain 'id' and 'message').
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: List of parsed log dictionaries containing mapped templates.
        """
        if start_time is None:
            start_time = time.perf_counter()

        parsed_results = {}
        history = []
        counters = {
            'log_volume': 0,
            'llm_invocations': 0,
            'cache_hits': 0
        }

        quarantine_path = 'data/parsed/quarantine.jsonl'
        os.makedirs(os.path.dirname(quarantine_path), exist_ok=True)

        buffer = collections.deque()
        oldest_log_time = None

        for log in log_list:
            buffer.append(log)
            if oldest_log_time is None:
                oldest_log_time = time.perf_counter()

            # Flush triggers:
            # 1. Volume: buffer size reaches max size
            # 2. Time: timeout has passed since oldest log entered
            current_time = time.perf_counter()
            if len(buffer) >= self.buffer_max_size or (current_time - oldest_log_time) >= self.flush_timeout:
                micro_batch = list(buffer)
                buffer.clear()
                oldest_log_time = None
                self._process_micro_batch(
                    micro_batch, parsed_results, quarantine_path,
                    start_time, time_limit, history, counters
                )

        if buffer:
            micro_batch = list(buffer)
            buffer.clear()
            self._process_micro_batch(
                micro_batch, parsed_results, quarantine_path,
                start_time, time_limit, history, counters
            )

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

    def _process_micro_batch(self, micro_batch, parsed_results, quarantine_path, start_time, time_limit, history, counters):
        current_time = time.perf_counter()
        elapsed = current_time - start_time
        if time_limit and elapsed > time_limit:
            logger.warning("Time limit exceeded. Skipping micro-batch.")
            return

        # 1. Cluster the micro-batch
        clusterer = get_clusterer(
            self.cluster_type, 
            micro_batch, 
            self.similarity_threshold, 
            self.vectorizer_type, 
            self.use_dynamic_eps
        )
        local_clusters = clusterer.get_partitions()

        # 2. Noise Quarantine Routing
        # Outlier logs (label -1) are filtered out, bypassing the LLM and Global Reconciliation entirely.
        noise_logs = getattr(clusterer, 'noise_logs', [])
        if noise_logs:
            logger.info(f"Quarantining {len(noise_logs)} noise logs to {quarantine_path}...")
            try:
                with open(quarantine_path, 'a', encoding='utf-8') as qf:
                    for log in noise_logs:
                        qf.write(json.dumps(log) + '\n')
            except Exception as e:
                logger.error(f"Error writing to quarantine.jsonl: {e}")

        # 3. Process each local cluster in queue
        queue = list(local_clusters)

        while queue:
            current_time = time.perf_counter()
            elapsed = current_time - start_time
            if time_limit and elapsed > time_limit:
                logger.warning("Time limit reached inside micro-batch processing.")
                break

            local_cluster = queue.pop(0)
            if not local_cluster:
                continue

            # Extract Medoid
            if hasattr(clusterer, 'get_medoid'):
                medoid_log = clusterer.get_medoid(local_cluster)
            else:
                medoid_log = local_cluster[0]

            # 4. Global Reconciliation against cache medoids
            best_match = None
            best_similarity = -1.0
            medoid_tokens = medoid_log['message'].split()

            for entry in self.cache.cache:
                ref_tokens = entry['ref_log'].split()
                sim = jaccard_similarity(medoid_tokens, ref_tokens)
                if sim > best_similarity:
                    best_similarity = sim
                    best_match = entry

            if best_match and best_similarity >= self.similarity_threshold:
                # Cache Hit: Route logs through cached template
                cached_template = best_match['template']
                best_match['frequency'] += 1
                self.cache.sort_cache()

                matched, pruned = match_and_prune(cached_template, local_cluster, self.cache)
                for log in matched:
                    parsed_results[log['id']] = cached_template

                counters['log_volume'] += len(matched)
                counters['cache_hits'] += len(matched)
                history.append({
                    'log_volume': counters['log_volume'],
                    'llm_invocations': counters['llm_invocations'],
                    'cache_hits': counters['cache_hits']
                })

                if pruned:
                    queue.append(pruned)
            else:
                # Cache Miss: Sample, batch LLM query, and register new Global Cluster
                sampled = self.sampler.sample(local_cluster, time_limit=time_limit, start_time=start_time)
                generated_template = self.parsing_base.batch_query(sampled)

                if generated_template:
                    matched, pruned = match_and_prune(generated_template, local_cluster, self.cache)
                    if len(matched) > 0:
                        for log in matched:
                            parsed_results[log['id']] = generated_template

                        # Medoid message becomes reference log
                        self.cache.add(generated_template, medoid_log['message'])

                        counters['log_volume'] += len(matched)
                        counters['llm_invocations'] += 1
                        history.append({
                            'log_volume': counters['log_volume'],
                            'llm_invocations': counters['llm_invocations'],
                            'cache_hits': counters['cache_hits']
                        })

                        if pruned:
                            queue.append(pruned)
                    else:
                        logger.warning("Generated template failed to match any logs in local cluster. Falling back to raw messages.")
                        for log in local_cluster:
                            parsed_results[log['id']] = log['message']

                        counters['log_volume'] += len(local_cluster)
                        counters['llm_invocations'] += 1
                        history.append({
                            'log_volume': counters['log_volume'],
                            'llm_invocations': counters['llm_invocations'],
                            'cache_hits': counters['cache_hits']
                        })
                else:
                    for log in local_cluster:
                        parsed_results[log['id']] = log['message']

                    counters['log_volume'] += len(local_cluster)
                    history.append({
                        'log_volume': counters['log_volume'],
                        'llm_invocations': counters['llm_invocations'],
                        'cache_hits': counters['cache_hits']
                    })
