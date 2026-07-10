"""Diversity and similarity sampling module for LogBatcher.

Implements Random, Determinantal Point Process (DPP) diversity, and Similarity (kNN) samplers
to pick representative log subsets for LLM batch template query tasks.
"""

import random
import numpy as np
import logging
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

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

    def sample(self, logs):
        """Randomly samples batch_size logs.

        Args:
            logs (list): List of logs.

        Returns:
            list: Random log subset.
        """
        if len(logs) <= self.batch_size:
            return logs
        return random.sample(logs, self.batch_size)

class DPPSampler(Sampler):
    """Samples log items using Determinantal Point Process (DPP) for diversity."""

    def __init__(self, llm_client, batch_size=10):
        """Initializes DPPSampler.

        Args:
            llm_client (OllamaClient): Client to retrieve embeddings.
            batch_size (int): Sample size. Defaults to 10.
        """
        super().__init__(batch_size)
        self.llm_client = llm_client

    def sample(self, logs, time_limit=None, start_time=None):
        """Performs greedy DPP selection on log embeddings.

        Args:
            logs (list): List of logs.
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: DPP-selected log subset.
        """
        if len(logs) <= self.batch_size:
            return logs

        import time

        # Pre-sample a candidate pool of up to 100 logs to prevent memory exhaustion (OOM)
        # and long computation times on huge partitions.
        max_candidates = 100
        if len(logs) > max_candidates:
            candidate_pool = random.sample(logs, max_candidates)
        else:
            candidate_pool = logs

        embeddings = []
        for idx, log in enumerate(candidate_pool):
            if time_limit and start_time and (time.perf_counter() - start_time) > time_limit:
                logger.warning("Sampler time budget exceeded during embedding extraction. Aborting sampler.")
                break

            msg = log.get('message', '')
            try:
                emb = self.llm_client.get_embedding(msg)
                embeddings.append(emb)
            except Exception as e:
                logger.error(f"Failed to get embedding: {e}")
                embeddings.append(np.random.rand(768).tolist())

        if not embeddings:
            return random.sample(logs, min(len(logs), self.batch_size))

        emb_matrix = np.array(embeddings)
        kernel_matrix = cosine_similarity(emb_matrix)

        n = kernel_matrix.shape[0]
        selected = []
        diag = np.diag(kernel_matrix)
        selected.append(np.argmax(diag))

        while len(selected) < self.batch_size:
            unselected = [i for i in range(n) if i not in selected]
            max_vol = -1
            best_i = -1

            for i in unselected:
                candidate_idx = selected + [i]
                submatrix = kernel_matrix[np.ix_(candidate_idx, candidate_idx)]
                vol = np.linalg.det(submatrix)
                if vol > max_vol:
                    max_vol = vol
                    best_i = i

            if best_i != -1:
                selected.append(best_i)
            else:
                break

        return [candidate_pool[i] for i in selected]

class SimilarSampler(Sampler):
    """Samples log items closest to the cluster medoid using Jaccard similarity."""

    def sample(self, logs):
        """Samples the medoid and the batch_size - 1 closest logs.

        Args:
            logs (list): List of logs in the cluster.

        Returns:
            list: Similar log subset.
        """
        if len(logs) <= self.batch_size:
            return logs

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
        return [logs[idx] for idx in sorted_indices[:self.batch_size]]

def get_sampler(sampler_type, llm_client, batch_size=10):
    """Factory function returning the configured Sampler instance.

    Args:
        sampler_type (str): Type of sampler ('DPPSampler', 'SimilarSampler', or 'RandomSampler').
        llm_client (OllamaClient): Connection client for embeddings.
        batch_size (int): Max sample size. Defaults to 10.

    Returns:
        Sampler: Configured sampler instance.

    Raises:
        ValueError: If the sampler type is unknown.
    """
    if sampler_type == "DPPSampler":
        return DPPSampler(llm_client, batch_size)
    elif sampler_type == "SimilarSampler":
        return SimilarSampler(batch_size)
    elif sampler_type == "RandomSampler":
        return RandomSampler(batch_size)
    else:
        raise ValueError(f"Unknown sampler type: {sampler_type}")
