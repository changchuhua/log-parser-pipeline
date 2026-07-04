from .cluster import Cluster

def jaccard_similarity(tokens1, tokens2):
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2:
        return 1.0
    return len(set1.intersection(set2)) / len(set1.union(set2))

class SimilarityCluster(Cluster):
    def __init__(self, logs, threshold=0.8):
        super().__init__(logs)
        self.threshold = threshold

    def get_partitions(self):
        length_groups = {}
        for log in self.logs:
            msg = log.get('message', '')
            tokens = msg.split()
            length = len(tokens)
            if length not in length_groups:
                length_groups[length] = []
            length_groups[length].append(log)

        all_clusters = []
        for length, len_logs in length_groups.items():
            length_clusters = []
            for log in len_logs:
                placed = False
                tokens = log.get('message', '').split()
                for c in length_clusters:
                    ref_tokens = c[0].get('message', '').split()
                    if jaccard_similarity(tokens, ref_tokens) >= self.threshold:
                        c.append(log)
                        placed = True
                        break
                if not placed:
                    length_clusters.append([log])
            all_clusters.extend(length_clusters)
            
        return all_clusters
