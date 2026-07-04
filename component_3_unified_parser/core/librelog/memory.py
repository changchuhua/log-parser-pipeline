def jaccard_similarity(str1, str2):
    tokens1 = str1.split()
    tokens2 = str2.split()
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2:
        return 1.0
    return len(set1.intersection(set2)) / len(set1.union(set2))

class LogMemory:
    def __init__(self, max_size=1000, similarity_threshold=0.85):
        self.max_size = max_size
        self.similarity_threshold = similarity_threshold
        self.memory = []
        
    def add(self, raw_log, template, group_key):
        if len(self.memory) >= self.max_size:
            self.memory.pop(0)
            
        for item in self.memory:
            if item["raw_log"] == raw_log:
                return
                
        self.memory.append({
            "raw_log": raw_log,
            "template": template,
            "group_key": group_key
        })
        
    def get_exact_match(self, raw_log, group_key):
        for item in self.memory:
            if item["group_key"] == group_key:
                if item["raw_log"] == raw_log:
                    return item["template"]
        return None

    def get_similar_logs(self, new_log, k, group_key):
        candidate_logs = [item for item in self.memory if item["group_key"] == group_key]
        if len(candidate_logs) < k:
            candidate_logs = self.memory
            
        scored_candidates = []
        for item in candidate_logs:
            sim = jaccard_similarity(new_log, item["raw_log"])
            scored_candidates.append((sim, item))
            
        scored_candidates.sort(key=lambda x: x[0], reverse=True)
        top_k = scored_candidates[:k]
        return [item for sim, item in top_k]
