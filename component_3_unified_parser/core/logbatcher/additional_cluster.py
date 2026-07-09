import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.feature_extraction.text import CountVectorizer
from scipy.spatial.distance import pdist, squareform
from .cluster import Cluster

class SimilarityCluster(Cluster):
    """Log partitioning using DBSCAN on a precomputed SciPy Jaccard distance matrix."""

    def __init__(self, logs, threshold=0.8):
        """Initializes the SimilarityCluster instance.

        Args:
            logs (list): List of logs.
            threshold (float): Similarity threshold (minimum Jaccard similarity).
        """
        super().__init__(logs)
        self.threshold = threshold
        self.noise_logs = []
        self.dist_matrix = None
        self.log_to_idx = {id(log): idx for idx, log in enumerate(logs)}

    def get_partitions(self):
        """Groups logs using DBSCAN with precomputed Jaccard distances.

        Returns:
            list: List of log lists, representing local clusters.
        """
        if not self.logs:
            return []

        messages = [log.get('message', '') for log in self.logs]

        # 1. Precompute Jaccard distance matrix using CountVectorizer + SciPy pdist
        try:
            vectorizer = CountVectorizer(binary=True, token_pattern=r'\S+')
            binary_matrix = vectorizer.fit_transform(messages).toarray()
            # SciPy pdist computes pairwise Jaccard distance (1.0 - Jaccard similarity)
            self.dist_matrix = squareform(pdist(binary_matrix, metric='jaccard'))
        except ValueError:
            # Fallback for empty vocabulary (e.g. all empty strings or spaces)
            N = len(messages)
            self.dist_matrix = np.zeros((N, N))
            for i in range(N):
                for j in range(i + 1, N):
                    d = 0.0 if messages[i] == messages[j] else 1.0
                    self.dist_matrix[i, j] = d
                    self.dist_matrix[j, i] = d

        # 2. Configure and run precomputed DBSCAN
        eps = 1.0 - self.threshold
        db = DBSCAN(eps=eps, min_samples=2, metric='precomputed')
        labels = db.fit_predict(self.dist_matrix)

        # 3. Group logs by cluster labels
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
