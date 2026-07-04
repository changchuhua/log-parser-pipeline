"""PrefixTree Strict and Loose router matching modules.

Implements prefix tree node traversal and Jaccard token similarity calculations
for log routing decisions.
"""

import yaml

def jaccard_similarity(tokens1, tokens2):
    """Computes Jaccard Similarity between two token sets.

    Args:
        tokens1 (list): First token list.
        tokens2 (list): Second token list.

    Returns:
        float: Computed similarity index (0.0 to 1.0).
    """
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2:
        return 1.0
    return len(set1.intersection(set2)) / len(set1.union(set2))

class Node:
    """Represents a single node in the prefix tree router."""

    def __init__(self, token):
        """Initializes PrefixTree Node.

        Args:
            token (str): The token represented by this node.
        """
        self.token = token
        self.children = {}
        self.cluster = None

class PrefixTree:
    """PrefixTree matcher routing log messages based on strict and loose token alignments."""

    def __init__(self, config_path='/app/config.yaml'):
        """Initializes PrefixTree.

        Args:
            config_path (str): Central configuration path. Defaults to '/app/config.yaml'.
        """
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        self.loose_match_threshold = config.get('logparser_llm', {}).get('loose_match_threshold', 0.8)
        self.root = Node(None)
        self.clusters = []  # List of templates
        
    def strict_match(self, tokens):
        """Traverses tree node children attempting an exact prefix route.

        Args:
            tokens (list): Split log message tokens.

        Returns:
            str: Matching template if successful, else None.
        """
        current = self.root
        for token in tokens:
            if token in current.children:
                current = current.children[token]
            elif '<*>' in current.children:
                current = current.children['<*>']
            elif current.children and any(k.startswith('<') and k.endswith('>') for k in current.children):
                matched = False
                for k in current.children:
                    if k.startswith('<') and k.endswith('>'):
                        current = current.children[k]
                        matched = True
                        break
                if not matched:
                    return None
            else:
                return None
        return current.cluster

    def loose_match(self, log_tokens):
        """Finds closest template cluster using token-level Jaccard similarity.

        Matches only templates of the same token length.

        Args:
            log_tokens (list): Split log message tokens.

        Returns:
            str: Closest cluster template if score exceeds threshold, else None.
        """
        best_cluster = None
        best_score = 0.0
        
        for cluster in self.clusters:
            template_tokens = cluster.split(' ')
            if len(template_tokens) == len(log_tokens):
                static_template_tokens = [t for t in template_tokens if not (t.startswith('<') and t.endswith('>'))]
                static_log_tokens = [log_tokens[i] for i, t in enumerate(template_tokens) if not (t.startswith('<') and t.endswith('>'))]
                
                score = jaccard_similarity(static_template_tokens, static_log_tokens)
                if score > best_score:
                    best_score = score
                    best_cluster = cluster
                    
        if best_score > self.loose_match_threshold:
            return best_cluster
        return None
        
    def insert(self, template):
        """Inserts a new template route sequence into the PrefixTree.

        Args:
            template (str): Normalized template content.
        """
        if template in self.clusters:
            return
        self.clusters.append(template)
        tokens = template.split(' ')
        current = self.root
        for token in tokens:
            if token not in current.children:
                current.children[token] = Node(token)
            current = current.children[token]
        current.cluster = template
