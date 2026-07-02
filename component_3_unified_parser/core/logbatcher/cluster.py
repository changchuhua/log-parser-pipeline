def jaccard_similarity(tokens1, tokens2):
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2: return 1.0
    return len(set1.intersection(set2)) / len(set1.union(set2))

class LogClusterer:
    def __init__(self, threshold=0.8):
        self.threshold = threshold
        
    def initial_partition(self, logs):
        clusters = []
        for log in logs:
            placed = False
            for cluster in clusters:
                ref_log = cluster[0]
                if len(log['tokens']) == len(ref_log['tokens']):
                    sim = jaccard_similarity(log['tokens'], ref_log['tokens'])
                    if sim >= self.threshold:
                        cluster.append(log)
                        placed = True
                        break
            if not placed:
                clusters.append([log])
        return clusters
