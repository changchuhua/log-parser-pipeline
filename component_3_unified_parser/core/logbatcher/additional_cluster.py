import re
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.feature_extraction.text import CountVectorizer, TfidfVectorizer
from sklearn.metrics.pairwise import cosine_distances
from scipy.spatial.distance import pdist, squareform
from .cluster import Cluster


def _tokenize_for_noise_grouping(log_content, tokenize_pattern=r'[ ,|]', remove_digits=True):
    """Faithful port of upstream LogBatcher's cluster.py::tokenize() -- used
    only for noise_mode: "original" exact-duplicate grouping below, not for
    DBSCAN vectorization (which uses CountVectorizer/TfidfVectorizer)."""
    words = re.split(tokenize_pattern, log_content)
    new_words = []
    for word in words:
        if '=' in word:
            ws = word.split('=')
            if len(ws) <= 2:
                new_words.append(ws[0])
        elif remove_digits and re.search(r'\d', word):
            pass
        elif '/' in word.lower() or re.match(r"^[a-zA-Z][+-]$|^[+-][a-zA-Z]$", word):
            pass
        else:
            word = re.sub(r"\([^)]*\)", "", word)
            new_words.append(word)
    new_words = [w for w in new_words if w]
    if not new_words:
        new_words.append(re.sub(r'\d+(\.\d+)?', '0', log_content))
    return new_words


def _reassign_noise_labels(labels, cluster_nums, messages):
    """Faithful port of upstream LogBatcher's cluster.py::reassign_clusters():
    groups exact-tokenized-string duplicates among -1-labeled entries into
    shared new cluster IDs; every remaining singleton gets its own new ID.
    Mutates and returns labels -- no -1 survives this pass."""
    tokenized = [' '.join(_tokenize_for_noise_grouping(m)) for m in messages]
    for i in range(len(labels)):
        if labels[i] == -1:
            for j in range(i + 1, len(labels)):
                if labels[j] == -1 and tokenized[i] == tokenized[j]:
                    labels[j] = cluster_nums
            labels[i] = cluster_nums
            cluster_nums += 1
    return labels, cluster_nums


class SimilarityCluster(Cluster):
    """Log partitioning using DBSCAN with configurable vectorizer and dynamic eps."""

    def __init__(self, logs, threshold=0.8, vectorizer_type="binary", use_dynamic_eps=False, noise_mode="production"):
        """Initializes the SimilarityCluster instance.

        Args:
            logs (list): List of logs.
            threshold (float): Similarity threshold (minimum Jaccard/Cosine similarity).
            vectorizer_type (str): Type of vectorization ('binary' or 'tfidf').
            use_dynamic_eps (bool): If True, adjust DBSCAN eps dynamically.
            noise_mode (str): "production" (default) leaves DBSCAN noise (-1)
                logs in self.noise_logs for the caller's own fallback handling.
                "original" reassigns them into new clusters in-place here (a
                faithful port of upstream's reassign_clusters()), so
                self.noise_logs stays empty and every log flows through the
                normal cluster pipeline instead.
        """
        super().__init__(logs)
        self.threshold = threshold
        self.vectorizer_type = vectorizer_type
        self.use_dynamic_eps = use_dynamic_eps
        self.noise_mode = noise_mode
        self.noise_logs = []
        self.dist_matrix = None
        self.log_to_idx = {id(log): idx for idx, log in enumerate(logs)}

    def get_partitions(self):
        """Groups logs using DBSCAN with precomputed distance matrix.

        Returns:
            list: List of log lists, representing local clusters.
        """
        if not self.logs:
            return []

        messages = [log.get('message', '') for log in self.logs]

        # 1. Compute distance matrix
        if self.vectorizer_type == "tfidf":
            try:
                vectorizer = TfidfVectorizer(token_pattern=r'\S+')
                tfidf_matrix = vectorizer.fit_transform(messages)
                self.dist_matrix = cosine_distances(tfidf_matrix)
            except ValueError:
                N = len(messages)
                self.dist_matrix = np.zeros((N, N))
        else:  # Default binary (CountVectorizer + Jaccard)
            try:
                vectorizer = CountVectorizer(binary=True, token_pattern=r'\S+')
                binary_matrix = vectorizer.fit_transform(messages).toarray()
                self.dist_matrix = squareform(pdist(binary_matrix, metric='jaccard'))
            except ValueError:
                # Fallback for empty vocabulary
                N = len(messages)
                self.dist_matrix = np.zeros((N, N))
                for i in range(N):
                    for j in range(i + 1, N):
                        d = 0.0 if messages[i] == messages[j] else 1.0
                        self.dist_matrix[i, j] = d
                        self.dist_matrix[j, i] = d

        # 2. Determine eps
        if self.use_dynamic_eps:
            token_lengths = [len(m.split()) for m in messages]
            std_dev = np.std(token_lengths) if len(token_lengths) > 1 else 0
            # Scale eps based on standard deviation of token lengths
            dynamic_eps = (1.0 - self.threshold) * (1.0 + 0.1 * std_dev)
            eps = min(max(dynamic_eps, 0.05), 0.5)
        else:
            eps = 1.0 - self.threshold

        # 3. DBSCAN clustering
        db = DBSCAN(eps=eps, min_samples=2, metric='precomputed')
        labels = db.fit_predict(self.dist_matrix)

        if self.noise_mode == 'original':
            cluster_nums = int(labels.max()) + 1 if len(labels) else 0
            labels, _ = _reassign_noise_labels(labels, cluster_nums, messages)
            # No label is -1 past this point -- self.noise_logs stays empty
            # and every log below lands in cluster_map instead.

        # 4. Group logs by cluster labels
        cluster_map = {}
        for idx, label in enumerate(labels):
            log = self.logs[idx]
            if label == -1:
                self.noise_logs.append(log)
            else:
                if label not in cluster_map:
                    cluster_map[label] = []
                cluster_map[label].append(log)

        return list(cluster_map.values())

    def get_medoid(self, cluster_logs):
        """Extracts the mathematical medoid of a cluster using the precomputed distance matrix.

        Args:
            cluster_logs (list): List of logs in the cluster.

        Returns:
            dict: The medoid log message object.
        """
        if not cluster_logs:
            return None
        indices = [self.log_to_idx[id(log)] for log in cluster_logs]
        if len(indices) == 1:
            return cluster_logs[0]

        # Slice the precomputed distance matrix for this cluster
        sub_matrix = self.dist_matrix[np.ix_(indices, indices)]
        # Sum distances along rows: the medoid minimizes the sum of distances to other members
        row_sums = np.sum(sub_matrix, axis=1)
        best_idx = np.argmin(row_sums)
        return cluster_logs[best_idx]
