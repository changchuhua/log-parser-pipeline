"""Diversity and similarity sampling module for LogBatcher.

Implements Random, Determinantal Point Process (DPP) diversity, and Similarity (kNN) samplers
to pick representative log subsets for LLM batch template query tasks.
"""

import random
import time
import numpy as np
import logging
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

def _jaccard_diverse_select(logs, count, time_limit=None, start_time=None):
    """Medoid + nearest-neighbor Jaccard diverse selection.

    Shared by SimilarSampler and DPPSampler's length/failure fallback path,
    so the medoid/distance-matrix logic exists in exactly one place.

    Args:
        logs (list): Candidate logs to select from.
        count (int): Number of logs to select.
        time_limit (float, optional): Maximum execution duration in seconds.
        start_time (float, optional): Parser execution start timestamp.

    Returns:
        list: Selected log subset, closest to the cluster medoid.
    """
    if len(logs) <= count:
        return logs
    if time_limit and start_time and (time.perf_counter() - start_time) > time_limit:
        logger.warning("Sampler time budget exceeded before Jaccard selection.")
        return random.sample(logs, count)

    # 1. Compute Jaccard distances between all pairs
    token_sets = [set(log.get('message', '').split()) for log in logs]
    N = len(logs)
    distances = np.zeros((N, N))

    for i in range(N):
        for j in range(i + 1, N):
            s1, s2 = token_sets[i], token_sets[j]
            union_len = len(s1.union(s2))
            sim = len(s1.intersection(s2)) / union_len if union_len > 0 else 1.0
            dist = 1.0 - sim
            distances[i, j] = dist
            distances[j, i] = dist

    # Medoid minimizes the sum of distances to other members
    row_sums = np.sum(distances, axis=1)
    medoid_idx = np.argmin(row_sums)

    # 2. Sort by distance to the medoid
    sorted_indices = np.argsort(distances[medoid_idx])
    return [logs[idx] for idx in sorted_indices[:count]]

class Sampler:
    """Base class for log sampling algorithms."""

    def __init__(self, batch_size=10):
        """Initializes Sampler base.

        Args:
            batch_size (int): Max number of samples to return. Defaults to 10.
        """
        self.batch_size = batch_size

    def sample(self, logs):
        """Abstract sampling method.

        Raises:
            NotImplementedError: If not implemented in sub-classes.
        """
        raise NotImplementedError()

class RandomSampler(Sampler):
    """Samples log items randomly from a partition."""

    def sample(self, logs, time_limit=None, start_time=None):
        """Randomly samples batch_size logs.

        Args:
            logs (list): List of logs.
            time_limit (float, optional): Unused; accepted for call-site parity with other samplers.
            start_time (float, optional): Unused; accepted for call-site parity with other samplers.

        Returns:
            list: Random log subset.
        """
        if len(logs) <= self.batch_size:
            return logs
        return random.sample(logs, self.batch_size)

class DPPSampler(Sampler):
    """Samples log items using Determinantal Point Process (DPP) for diversity.

    Logs longer than embedding_length_threshold (or that fail to embed for any
    other reason) skip embedding entirely and are instead selected via Jaccard
    diverse selection (_jaccard_diverse_select). This avoids substituting a
    random embedding vector for logs that can't be embedded: a random vector
    looks spuriously "diverse" next to real embeddings in the DPP kernel,
    which biases selection toward including exactly the logs that failed.
    """

    def __init__(self, llm_client, batch_size=10, embedding_length_threshold=4000):
        """Initializes DPPSampler.

        Args:
            llm_client (OllamaClient): Client to retrieve embeddings.
            batch_size (int): Sample size. Defaults to 10.
            embedding_length_threshold (int, optional): Character-length cutoff
                above which a log skips embedding and is routed to the Jaccard
                fallback instead. None disables length-based routing (every
                log still attempts embedding; embedding failures still route
                to the fallback).
        """
        super().__init__(batch_size)
        self.llm_client = llm_client
        self.embedding_length_threshold = embedding_length_threshold

    def sample(self, logs, time_limit=None, start_time=None):
        """Performs greedy DPP selection on log embeddings.

        Args:
            logs (list): List of logs.
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: DPP-selected log subset, supplemented with Jaccard-selected
                long/failed-embedding logs if DPP alone doesn't fill batch_size.
        """
        if len(logs) <= self.batch_size:
            return logs

        # Pre-sample a candidate pool of up to 100 logs to prevent memory exhaustion (OOM)
        # and long computation times on huge partitions.
        max_candidates = 100
        if len(logs) > max_candidates:
            candidate_pool = random.sample(logs, max_candidates)
        else:
            candidate_pool = logs

        threshold = self.embedding_length_threshold
        if threshold is not None:
            short_pool = [log for log in candidate_pool if len(log.get('message', '')) <= threshold]
            long_pool = [log for log in candidate_pool if len(log.get('message', '')) > threshold]
        else:
            short_pool, long_pool = list(candidate_pool), []

        embeddings = []
        embedded_logs = []
        for idx, log in enumerate(short_pool):
            if time_limit and start_time and (time.perf_counter() - start_time) > time_limit:
                logger.warning("Sampler time budget exceeded during embedding extraction.")
                # Unembedded remainder still gets a chance via the Jaccard fallback below.
                long_pool.extend(short_pool[idx:])
                break

            msg = log.get('message', '')
            try:
                emb = self.llm_client.get_embedding(msg)
            except Exception as e:
                logger.error(f"Failed to get embedding: {e}; routing to Jaccard fallback instead of a random vector")
                long_pool.append(log)
                continue
            embeddings.append(emb)
            embedded_logs.append(log)

        selected = []
        if embeddings:
            emb_matrix = np.array(embeddings)
            kernel_matrix = cosine_similarity(emb_matrix)

            n = kernel_matrix.shape[0]
            idxs = []
            diag = np.diag(kernel_matrix)
            idxs.append(int(np.argmax(diag)))

            while len(idxs) < self.batch_size:
                unselected = [i for i in range(n) if i not in idxs]
                if not unselected:
                    break
                max_vol = -1
                best_i = -1

                for i in unselected:
                    candidate_idx = idxs + [i]
                    submatrix = kernel_matrix[np.ix_(candidate_idx, candidate_idx)]
                    vol = np.linalg.det(submatrix)
                    if vol > max_vol:
                        max_vol = vol
                        best_i = i

                if best_i != -1:
                    idxs.append(best_i)
                else:
                    break

            selected = [embedded_logs[i] for i in idxs]

        remaining = self.batch_size - len(selected)
        if remaining > 0 and long_pool:
            selected += _jaccard_diverse_select(long_pool, remaining, time_limit=time_limit, start_time=start_time)

        if not selected:
            return random.sample(logs, min(len(logs), self.batch_size))

        return selected

class SimilarSampler(Sampler):
    """Samples log items closest to the cluster medoid using Jaccard similarity."""

    def sample(self, logs, time_limit=None, start_time=None):
        """Samples the medoid and the batch_size - 1 closest logs.

        Args:
            logs (list): List of logs in the cluster.
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: Similar log subset.
        """
        return _jaccard_diverse_select(logs, self.batch_size, time_limit=time_limit, start_time=start_time)

def get_sampler(sampler_type, llm_client, batch_size=10, embedding_length_threshold=4000):
    """Factory function returning the configured Sampler instance.

    Args:
        sampler_type (str): Type of sampler ('DPPSampler', 'SimilarSampler', or 'RandomSampler').
        llm_client (OllamaClient): Connection client for embeddings.
        batch_size (int): Max sample size. Defaults to 10.
        embedding_length_threshold (int, optional): Passed to DPPSampler only —
            character-length cutoff above which a log skips embedding in favor
            of the Jaccard fallback. Defaults to 4000.

    Returns:
        Sampler: Configured sampler instance.

    Raises:
        ValueError: If the sampler type is unknown.
    """
    if sampler_type == "DPPSampler":
        return DPPSampler(llm_client, batch_size, embedding_length_threshold=embedding_length_threshold)
    elif sampler_type == "SimilarSampler":
        return SimilarSampler(batch_size)
    elif sampler_type == "RandomSampler":
        return RandomSampler(batch_size)
    else:
        raise ValueError(f"Unknown sampler type: {sampler_type}")
