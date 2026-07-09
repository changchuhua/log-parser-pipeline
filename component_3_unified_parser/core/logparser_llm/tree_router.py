"""PrefixTree Strict and Loose router matching modules.

Implements prefix tree node traversal and Jaccard token similarity calculations
for log routing decisions.
"""

import time
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
    union_len = len(set1.union(set2))
    if union_len == 0:
        return 0.0
    return len(set1.intersection(set2)) / union_len

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
        self.last_matched = None  # Float timestamp tracking matching/insertion recency

class PrefixTree:
    """PrefixTree matcher routing log messages based on strict and loose token alignments."""

    def __init__(self, config_path='/app/config.yaml'):
        """Initializes PrefixTree.

        Args:
            config_path (str): Central configuration path. Defaults to '/app/config.yaml'.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}
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

        # Exact match found, update its matched timestamp
        if current.cluster is not None:
            current.last_matched = time.time()
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
            # Match found, update its matched timestamp in the leaf node
            self.update_last_matched(best_cluster)
            return best_cluster
        return None

    def update_last_matched(self, template):
        """Updates the last_matched timestamp for a template's leaf node."""
        tokens = template.split(' ')
        current = self.root
        for token in tokens:
            if token in current.children:
                current = current.children[token]
            else:
                return
        current.last_matched = time.time()

    def insert(self, template):
        """Inserts a new template route sequence into the PrefixTree.

        Args:
            template (str): Normalized template content.
        """
        if template in self.clusters:
            self.update_last_matched(template)
            return
        self.clusters.append(template)
        tokens = template.split(' ')
        current = self.root
        for token in tokens:
            if token not in current.children:
                current.children[token] = Node(token)
            current = current.children[token]
        current.cluster = template
        current.last_matched = time.time()

    def prune_inactive_templates(self, current_time=None, max_age_seconds=30*24*3600):
        """Recursively prunes tree branches and clusters that are older than max_age_seconds."""
        if current_time is None:
            current_time = time.time()
        cutoff = current_time - max_age_seconds

        def traverse(node):
            child_tokens = list(node.children.keys())
            for t in child_tokens:
                traverse(node.children[t])
                # If child has no children and has no cluster, delete it to free RAM
                if not node.children[t].children and node.children[t].cluster is None:
                    del node.children[t]

            # If leaf node expires, clear cluster reference to let traversal delete it
            if node.cluster is not None:
                if node.last_matched is not None and node.last_matched < cutoff:
                    if node.cluster in self.clusters:
                        self.clusters.remove(node.cluster)
                    node.cluster = None
                    node.last_matched = None

        traverse(self.root)
