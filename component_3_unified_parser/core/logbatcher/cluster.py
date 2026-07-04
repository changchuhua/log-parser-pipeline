class Cluster:
    def __init__(self, logs):
        self.logs = logs

    def get_partitions(self):
        raise NotImplementedError()

class LengthCluster(Cluster):
    def get_partitions(self):
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
    if cluster_type == "LengthCluster":
        return LengthCluster(logs)
    elif cluster_type == "SimilarityCluster":
        from .additional_cluster import SimilarityCluster
        return SimilarityCluster(logs, threshold)
    else:
        raise ValueError(f"Unknown cluster type: {cluster_type}")
