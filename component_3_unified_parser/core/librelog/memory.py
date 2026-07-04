"""In-memory cache and similarity lookup repository for LibreLog.

Retains log template assignments and returns matching candidates based on group
routing and token Jaccard similarity.
"""

def jaccard_similarity(str1, str2):
    """Computes token Jaccard similarity of two space-separated string messages.

    Args:
        str1 (str): First string.
        str2 (str): Second string.

    Returns:
        float: Computed similarity (0.0 to 1.0).
    """
    tokens1 = str1.split()
    tokens2 = str2.split()
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2:
        return 1.0
    return len(set1.intersection(set2)) / len(set1.union(set2))

class LogMemory:
    """Bounded, group-aware memory cache lookup for log parsing templates."""

    def __init__(self, max_size=1000, similarity_threshold=0.85):
        """Initializes LogMemory.

        Args:
            max_size (int): Max number of cache items to hold. Defaults to 1000.
            similarity_threshold (float): Threshold similarity value. Defaults to 0.85.
        """
        self.max_size = max_size
        self.similarity_threshold = similarity_threshold
        self.memory = []
        
    def add(self, raw_log, template, group_key):
        """Appends a new template assignment mapping to the cache.

        Evicts oldest record if cache size exceeds max_size.

        Args:
            raw_log (str): Masked log message.
            template (str): Discovered template string.
            group_key (tuple): Mapped grouping partition key.
        """
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
        """Checks for an exact match of the raw log string inside the group key.

        Args:
            raw_log (str): Masked log message.
            group_key (tuple): Grouping key.

        Returns:
            str: Matching template if found, else None.
        """
        for item in self.memory:
            if item["group_key"] == group_key:
                if item["raw_log"] == raw_log:
                    return item["template"]
        return None

    def get_similar_logs(self, new_log, k, group_key):
        """Finds top-K most similar log templates inside the group.

        If group candidates are too few, fall back to searching across the full
        unpartitioned memory cache.

        Args:
            new_log (str): Target masked log message.
            k (int): Number of similar examples to retrieve.
            group_key (tuple): Grouping partition key.

        Returns:
            list: List of top K similar template dictionary records.
        """
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
