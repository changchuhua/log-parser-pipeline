"""Template calibration and merge manager for LogParser-LLM.

Aggregates similar parsed templates by checking token structural Jaccard similarity
and recalibrating the PrefixTree routing structures.
"""

import yaml

def string_structural_similarity(t1_tokens, t2_tokens):
    """Calculates token-level structural similarity of two template lists.

    Considers both variable token mappings and literal matches.

    Args:
        t1_tokens (list): Split tokens of first template.
        t2_tokens (list): Split tokens of second template.

    Returns:
        float: Similarity ratio between 0.0 and 1.0.
    """
    if len(t1_tokens) != len(t2_tokens):
        return 0.0
    
    matches = 0
    for tok1, tok2 in zip(t1_tokens, t2_tokens):
        is_var1 = tok1.startswith('<') and tok1.endswith('>')
        is_var2 = tok2.startswith('<') and tok2.endswith('>')
        
        if is_var1 and is_var2:
            matches += 1 
        elif tok1 == tok2:
            matches += 1
            
    return matches / len(t1_tokens)

class TemplateManager:
    """Manages merging and calibration of PrefixTree templates."""

    def __init__(self, tree_router, config_path='/app/config.yaml'):
        """Initializes TemplateManager.

        Args:
            tree_router (PrefixTree): Targets tree router references.
            config_path (str): YAML configuration path. Defaults to '/app/config.yaml'.
        """
        self.tree_router = tree_router
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}
        self.merge_similarity_threshold = config.get('logparser_llm', {}).get('merge_similarity_threshold', 0.95)
        
    def calibrate(self):
        """Merges overlapping clusters in PrefixTree by substituting mismatching tokens with <*>."""
        clusters = self.tree_router.clusters
        merged_clusters = []
        skip_indices = set()
        
        for i in range(len(clusters)):
            if i in skip_indices:
                continue
            
            base_template = clusters[i]
            base_tokens = base_template.split(' ')
            current_merged_tokens = list(base_tokens)
            
            for j in range(i + 1, len(clusters)):
                if j in skip_indices:
                    continue
                
                compare_template = clusters[j]
                compare_tokens = compare_template.split(' ')
                
                if len(base_tokens) != len(compare_tokens):
                    continue
                    
                sim = string_structural_similarity(current_merged_tokens, compare_tokens)
                if sim >= self.merge_similarity_threshold:
                    # Substitute mismatched tokens with <*>
                    for k in range(len(current_merged_tokens)):
                        if current_merged_tokens[k] != compare_tokens[k]:
                            current_merged_tokens[k] = '<*>'
                    skip_indices.add(j)
            
            merged_template = " ".join(current_merged_tokens)
            merged_clusters.append(merged_template)
            
        # Re-insert clean, deduplicated consolidated templates
        unique_merged = list(set(merged_clusters))
        
        self.tree_router.clusters = []
        self.tree_router.root.children = {}
        for tmpl in unique_merged:
            self.tree_router.insert(tmpl)
