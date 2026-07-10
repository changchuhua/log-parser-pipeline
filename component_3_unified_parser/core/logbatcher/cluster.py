"""Log partitioning/clustering modules for LogBatcher.

Implements token length clustering (LengthCluster) and similarity routing models
to cluster similar logs.
"""

class Cluster:
    """Base class for log clustering methods."""

    def __init__(self, logs):
        """Initializes Cluster base.

        Args:
            logs (list): List of log dictionary records.
        """
        self.logs = logs

    def get_partitions(self):
        """Partitions the logs into clusters.

        Raises:
            NotImplementedError: If not implemented in sub-classes.
        """
        raise NotImplementedError()

class LengthCluster(Cluster):
    """Log clustering based on token length and dataset source."""

    def get_partitions(self):
        """Groups log messages by their dataset source and split token length.

        Returns:
            list: List of log lists, partitioned by dataset and token count.
        """
        partitions = {}
        for log in self.logs:
            msg = log.get('message', '')
            tokens = msg.split()
            length = len(tokens)
            event_id = log.get('event', {}).get('id', '')
            dataset = event_id.split('_')[0] if event_id else 'unknown'
            key = (dataset, length)
            if key not in partitions:
                partitions[key] = []
            partitions[key].append(log)
        return list(partitions.values())

def get_clusterer(cluster_type, logs, threshold=0.8, vectorizer_type="binary", use_dynamic_eps=False):
    """Factory method to get the specified clustering instance.

    Args:
        cluster_type (str): Type of clusterer ('LengthCluster' or 'SimilarityCluster').
        logs (list): List of logs.
        threshold (float): Similarity threshold for similarity clustering. Defaults to 0.8.
        vectorizer_type (str): Type of vectorization ('binary' or 'tfidf').
        use_dynamic_eps (bool): If True, adjust DBSCAN eps dynamically.

    Returns:
        Cluster: Sub-class instance of Cluster.

    Raises:
        ValueError: If the clusterer type is unknown.
    """
    if cluster_type == "LengthCluster":
        return LengthCluster(logs)
    elif cluster_type == "SimilarityCluster":
        from .additional_cluster import SimilarityCluster
        return SimilarityCluster(logs, threshold, vectorizer_type, use_dynamic_eps)
    else:
        raise ValueError(f"Unknown cluster type: {cluster_type}")
