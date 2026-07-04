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
    """Log clustering based on token length."""

    def get_partitions(self):
        """Groups log messages by their split token length.

        Returns:
            list: List of log lists, partitioned by token count.
        """
        partitions = {}
        for log in self.logs:
            msg = log.get('message', '')
            tokens = msg.split()
            length = len(tokens)
            if length not in partitions:
                partitions[length] = []
            partitions[length].append(log)
        return list(partitions.values())

def get_clusterer(cluster_type, logs, threshold=0.8):
    """Factory method to get the specified clustering instance.

    Args:
        cluster_type (str): Type of clusterer ('LengthCluster' or 'SimilarityCluster').
        logs (list): List of logs.
        threshold (float): Similarity threshold for similarity clustering. Defaults to 0.8.

    Returns:
        Cluster: Sub-class instance of Cluster.

    Raises:
        ValueError: If the clusterer type is unknown.
    """
    if cluster_type == "LengthCluster":
        return LengthCluster(logs)
    elif cluster_type == "SimilarityCluster":
        from .additional_cluster import SimilarityCluster
        return SimilarityCluster(logs, threshold)
    else:
        raise ValueError(f"Unknown cluster type: {cluster_type}")
