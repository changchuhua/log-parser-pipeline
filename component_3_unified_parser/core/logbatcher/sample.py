"""Diversity and similarity sampling module for LogBatcher.

Implements Random, Determinantal Point Process (DPP) diversity, and Similarity (kNN) samplers
to pick representative log subsets for LLM batch template query tasks.
"""

import random
import time
import numpy as np
import logging
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans

logger = logging.getLogger(__name__)


def _group_samples_clustering(embed_matrix, num_in_batch):
    """Faithful port of upstream LogBatcher's sample.py::group_samples_clustering().

    K-means clusters embed_matrix into ceil(N/num_in_batch) groups, then
    rebalances any over-full group by reassigning its lowest-similarity-to-
    centroid members to the next-best under-full group. Returns a list of
    index groups.
    """
    def _calculate_cos_similarities(v1, v2):
        num = np.dot(v1, v2.T)
        denom = np.linalg.norm(v1, axis=1).reshape(-1, 1) * np.linalg.norm(v2, axis=1)
        similarity_matrix = num / denom
        similarity_matrix[np.isneginf(similarity_matrix)] = 0
        similarity_matrix = 0.5 + 0.5 * similarity_matrix
        return similarity_matrix

    if embed_matrix.shape[0] % num_in_batch:
        n_clusters = embed_matrix.shape[0] // num_in_batch + 1
    else:
        n_clusters = embed_matrix.shape[0] // num_in_batch
    n_clusters = max(n_clusters, 1)

    kmeans = KMeans(n_clusters=n_clusters, random_state=0, n_init="auto").fit(embed_matrix)
    similarity_matrix = _calculate_cos_similarities(embed_matrix, kmeans.cluster_centers_)
    similarity_rankings = np.argsort(-similarity_matrix, axis=1)
    groups = [[] for _ in range(n_clusters)]
    for sample_idx, label in enumerate(kmeans.labels_):
        groups[label].append(sample_idx)

    for group_idx, group in enumerate(groups):
        if len(group) > num_in_batch:
            groups[group_idx] = sorted(group, key=lambda x: similarity_matrix[x, group_idx], reverse=True)
            samples_to_reassign = groups[group_idx][num_in_batch:]
            groups[group_idx] = groups[group_idx][:num_in_batch]
            for sample_idx in samples_to_reassign:
                for candi_group_idx in similarity_rankings[sample_idx]:
                    if len(groups[candi_group_idx]) < num_in_batch:
                        groups[candi_group_idx].append(sample_idx)
                        break
    return groups

def _greedy_dpp_select(kernel_matrix, k):
    """Greedy max-determinant DPP selection -- shared by both DPPSampler
    kernel-source paths (embedding-based and TF-IDF-based).

    Mathematically identical to upstream LogBatcher's sample.py::dpp_sample():
    upstream picks argmax(det_Yi / (1 + det_Yi)) at each step, which is
    monotonic in det_Yi for det_Yi >= 0, so maximizing one maximizes the
    other -- both pick the same index at each step given the same kernel.
    """
    n = kernel_matrix.shape[0]
    idxs = [int(np.argmax(np.diag(kernel_matrix)))]
    while len(idxs) < k:
        unselected = [i for i in range(n) if i not in idxs]
        if not unselected:
            break
        best_i, max_vol = -1, -1
        for i in unselected:
            vol = np.linalg.det(kernel_matrix[np.ix_(idxs + [i], idxs + [i])])
            if vol > max_vol:
                max_vol, best_i = vol, i
        if best_i == -1:
            break
        idxs.append(best_i)
    return idxs


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

    Two kernel sources, selected via dpp_kernel_mode:

    "production" (default): LLM-embedding kernel. Logs longer than
    embedding_length_threshold (or that fail to embed for any other reason)
    skip embedding entirely and are instead selected via Jaccard diverse
    selection (_jaccard_diverse_select). This avoids substituting a random
    embedding vector for logs that can't be embedded: a random vector looks
    spuriously "diverse" next to real embeddings in the DPP kernel, which
    biases selection toward including exactly the logs that failed.

    "original": faithful port of upstream LogBatcher's actual kernel source
    (Cluster.sample() in cluster.py) -- TF-IDF vectors of the log text, not
    embeddings. No embedding calls, no length threshold, no fallback needed:
    TF-IDF vectorization has no failure mode analogous to an embedding API
    call or context-length limit. The greedy selection algorithm itself
    (_greedy_dpp_select) is identical either way -- only the kernel differs.
    """

    def __init__(self, llm_client, batch_size=10, embedding_length_threshold=4000, dpp_kernel_mode="production"):
        """Initializes DPPSampler.

        Args:
            llm_client (OllamaClient): Client to retrieve embeddings. Unused
                when dpp_kernel_mode="original".
            batch_size (int): Sample size. Defaults to 10.
            embedding_length_threshold (int, optional): Character-length cutoff
                above which a log skips embedding and is routed to the Jaccard
                fallback instead. None disables length-based routing (every
                log still attempts embedding; embedding failures still route
                to the fallback). Only relevant to dpp_kernel_mode="production".
            dpp_kernel_mode (str): "production" (default, embedding kernel) or
                "original" (TF-IDF kernel, faithful to upstream's Cluster.sample()).
        """
        super().__init__(batch_size)
        self.llm_client = llm_client
        self.embedding_length_threshold = embedding_length_threshold
        self.dpp_kernel_mode = dpp_kernel_mode

    def sample(self, logs, time_limit=None, start_time=None):
        """Performs greedy DPP selection on a similarity kernel.

        Args:
            logs (list): List of logs.
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: DPP-selected log subset. Under dpp_kernel_mode="production",
                supplemented with Jaccard-selected long/failed-embedding logs
                if DPP alone doesn't fill batch_size.
        """
        if len(logs) <= self.batch_size:
            return logs

        # Pre-sample a candidate pool of up to 100 logs to prevent memory exhaustion (OOM)
        # and long computation times on huge partitions. Kept for both kernel
        # modes -- this is about memory safety, not fidelity to either
        # mechanism, and upstream itself simply lacks any safeguard here.
        max_candidates = 100
        if len(logs) > max_candidates:
            candidate_pool = random.sample(logs, max_candidates)
        else:
            candidate_pool = logs

        if self.dpp_kernel_mode == 'original':
            return self._sample_original_kernel(logs, candidate_pool)
        return self._sample_production_kernel(logs, candidate_pool, time_limit, start_time)

    def _sample_original_kernel(self, logs, candidate_pool):
        messages = [log.get('message', '') for log in candidate_pool]
        try:
            tfidf_matrix = TfidfVectorizer().fit_transform(messages).toarray()
        except ValueError:
            # Empty vocabulary (e.g. all-whitespace messages) -- no basis for
            # TF-IDF, fall back to a random subset rather than crash.
            return random.sample(logs, min(len(logs), self.batch_size))
        kernel_matrix = cosine_similarity(tfidf_matrix)
        idxs = _greedy_dpp_select(kernel_matrix, self.batch_size)
        return [candidate_pool[i] for i in idxs]

    def _sample_production_kernel(self, logs, candidate_pool, time_limit, start_time):
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
            idxs = _greedy_dpp_select(kernel_matrix, self.batch_size)
            selected = [embedded_logs[i] for i in idxs]

        remaining = self.batch_size - len(selected)
        if remaining > 0 and long_pool:
            selected += _jaccard_diverse_select(long_pool, remaining, time_limit=time_limit, start_time=start_time)

        if not selected:
            return random.sample(logs, min(len(logs), self.batch_size))

        return selected

class SimilarSampler(Sampler):
    """Samples log items closest to the cluster medoid using Jaccard similarity."""

    def __init__(self, batch_size=10, mode="production"):
        """Initializes SimilarSampler.

        Args:
            batch_size (int): Max number of samples to return. Defaults to 10.
            mode (str): "production" (default) uses medoid + Jaccard nearest-
                neighbor selection. "original" faithfully ports upstream's
                actual "similar" sample_method -- K-means clustering on TF-IDF
                vectors of the message text (group_samples_clustering()),
                taking the first returned group like upstream's own call site
                does. No embedding model dependency either way.
        """
        super().__init__(batch_size)
        self.mode = mode

    def sample(self, logs, time_limit=None, start_time=None):
        """Samples a representative log subset.

        Args:
            logs (list): List of logs in the cluster.
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: Similar log subset.
        """
        if self.mode == 'original':
            return self._sample_original(logs)
        return _jaccard_diverse_select(logs, self.batch_size, time_limit=time_limit, start_time=start_time)

    def _sample_original(self, logs):
        if len(logs) <= self.batch_size:
            return logs
        messages = [log.get('message', '') for log in logs]
        try:
            tfidf_matrix = TfidfVectorizer().fit_transform(messages).toarray()
        except ValueError:
            # Empty vocabulary (e.g. all-whitespace messages) -- no basis for
            # TF-IDF clustering, fall back to the production path.
            return _jaccard_diverse_select(logs, self.batch_size)
        groups = _group_samples_clustering(tfidf_matrix, self.batch_size)
        first_group = groups[0] if groups else []
        return [logs[i] for i in first_group]

def get_sampler(sampler_type, llm_client, batch_size=10, embedding_length_threshold=4000, similar_sampler_mode="production", dpp_kernel_mode="production"):
    """Factory function returning the configured Sampler instance.

    Args:
        sampler_type (str): Type of sampler ('DPPSampler', 'SimilarSampler', or 'RandomSampler').
        llm_client (OllamaClient): Connection client for embeddings.
        batch_size (int): Max sample size. Defaults to 10.
        embedding_length_threshold (int, optional): Passed to DPPSampler only —
            character-length cutoff above which a log skips embedding in favor
            of the Jaccard fallback. Defaults to 4000.
        similar_sampler_mode (str): Passed to SimilarSampler only -- see
            SimilarSampler.__init__. Harmlessly ignored for other sampler types.
        dpp_kernel_mode (str): Passed to DPPSampler only -- see
            DPPSampler.__init__. Harmlessly ignored for other sampler types.

    Returns:
        Sampler: Configured sampler instance.

    Raises:
        ValueError: If the sampler type is unknown.
    """
    if sampler_type == "DPPSampler":
        return DPPSampler(llm_client, batch_size, embedding_length_threshold=embedding_length_threshold, dpp_kernel_mode=dpp_kernel_mode)
    elif sampler_type == "SimilarSampler":
        return SimilarSampler(batch_size, mode=similar_sampler_mode)
    elif sampler_type == "RandomSampler":
        return RandomSampler(batch_size)
    else:
        raise ValueError(f"Unknown sampler type: {sampler_type}")
