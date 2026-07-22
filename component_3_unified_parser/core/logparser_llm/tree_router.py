"""PrefixTree Strict and Loose router matching modules.

Implements prefix tree node traversal and Jaccard token similarity calculations
for log routing decisions.
"""

import time
import yaml
import math

def weighted_jaccard_similarity(tokens1, tokens2, decay_factor=0.15):
    """Computes position-weighted Jaccard similarity with exponential decay."""
    if not tokens1 or not tokens2:
        return 0.0
    L = min(len(tokens1), len(tokens2))
    intersection_weight = 0.0
    union_weight = 0.0
    for i in range(L):
        w_i = math.exp(-decay_factor * i)
        if tokens1[i] == tokens2[i]:
            intersection_weight += w_i
        union_weight += w_i
    if union_weight == 0:
        return 0.0
    return intersection_weight / union_weight

def positional_uniform_similarity(tokens1, tokens2):
    """Computes simple positional matching ratio from the original paper."""
    if not tokens1 or not tokens2:
        return 0.0
    L = min(len(tokens1), len(tokens2))
    if L == 0:
        return 0.0
    match_count = sum(1 for i in range(L) if tokens1[i] == tokens2[i])
    return match_count / L

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
        lp_config = config.get('logparser_llm', {})
        self.loose_match_threshold = lp_config.get('loose_match_threshold', 0.8)
        metric = lp_config.get('loose_match_metric', None)
        if metric is not None:
            self.loose_match_metric = metric
        else:
            self.loose_match_metric = "positional_decay" if lp_config.get('use_positional_weighting', True) else "jaccard"
        self.decay_factor = lp_config.get('decay_factor', 0.15)
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

                if self.loose_match_metric == "positional_decay":
                    score = weighted_jaccard_similarity(static_template_tokens, static_log_tokens, self.decay_factor)
                elif self.loose_match_metric == "positional_uniform":
                    score = positional_uniform_similarity(static_template_tokens, static_log_tokens)
                else:
                    score = jaccard_similarity(static_template_tokens, static_log_tokens)

                if score > best_score:
                    best_score = score
                    best_cluster = cluster

        if best_score > self.loose_match_threshold:
            # Match found, update its matched timestamp in the leaf node
            self.update_last_matched(best_cluster)
            return best_cluster
        return None

    def get_loose_match_candidates(self, log_tokens, top_n=3):
        """Returns up to top_n templates that loosely matched log_tokens, sorted by
        score descending -- unlike loose_match(), which collapses to a single
        winner-or-None decision, this exposes the candidate set itself.

        Used by main_parser.py's match_llm_mode="original" path: the paper's
        Algorithm 1 always queries the LLM on a non-strict match, then attempts an
        inline merge-check against whichever clusters the loose match identified as
        candidates -- unlike match_llm_mode="production", where a loose match is
        itself the final answer and no merge-check ever runs. Read-only: does not
        update last_matched, since identifying a candidate isn't the same as using it.

        Args:
            log_tokens (list): Split log message tokens.
            top_n (int): Maximum number of candidates to return.

        Returns:
            list[str]: Candidate templates, best-scoring first.
        """
        scored = []
        for cluster in self.clusters:
            template_tokens = cluster.split(' ')
            if len(template_tokens) == len(log_tokens):
                static_template_tokens = [t for t in template_tokens if not (t.startswith('<') and t.endswith('>'))]
                static_log_tokens = [log_tokens[i] for i, t in enumerate(template_tokens) if not (t.startswith('<') and t.endswith('>'))]

                if self.loose_match_metric == "positional_decay":
                    score = weighted_jaccard_similarity(static_template_tokens, static_log_tokens, self.decay_factor)
                elif self.loose_match_metric == "positional_uniform":
                    score = positional_uniform_similarity(static_template_tokens, static_log_tokens)
                else:
                    score = jaccard_similarity(static_template_tokens, static_log_tokens)

                if score > self.loose_match_threshold:
                    scored.append((score, cluster))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [cluster for _, cluster in scored[:top_n]]

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
        """Iteratively prunes tree branches and clusters that are older than max_age_seconds."""
        if current_time is None:
            current_time = time.time()
        cutoff = current_time - max_age_seconds

        # 1. Level-order collection (BFS) of all parent-child links in the PrefixTree
        # queue stores tuples of (node, parent, token_key)
        queue = [(self.root, None, None)]
        idx = 0
        while idx < len(queue):
            node, parent, token = queue[idx]
            idx += 1
            for t, child in node.children.items():
                queue.append((child, node, t))

        # 2. Process collected nodes in reverse order (bottom-up/leaves-first)
        for node, parent, token in reversed(queue):
            # If leaf node expires, clear its cluster reference
            if node.cluster is not None:
                if node.last_matched is not None and node.last_matched < cutoff:
                    if node.cluster in self.clusters:
                        self.clusters.remove(node.cluster)
                    node.cluster = None
                    node.last_matched = None

            # If this is not the root, and this branch has no remaining children
            # and no active cluster, remove it from the parent node
            if parent is not None:
                if not node.children and node.cluster is None:
                    if token in parent.children:
                        del parent.children[token]

    def prune_to_capacity(self, max_templates=1000):
        """Evicts the least recently matched templates until tree size is within capacity limits."""
        if len(self.clusters) <= max_templates:
            return
            
        # 1. Sort active templates by last_matched leaf node timestamps (ascending order)
        template_timestamps = []
        for template in self.clusters:
            tokens = template.split(' ')
            current = self.root
            for token in tokens:
                if token in current.children:
                    current = current.children[token]
            
            # Use current time as fallback if last_matched is not set
            last_time = current.last_matched if current.last_matched is not None else 0.0
            template_timestamps.append((template, last_time))
            
        # Sort by timestamp ascending (oldest first)
        template_timestamps.sort(key=lambda x: x[1])
        
        # Evict oldest templates
        num_to_evict = len(self.clusters) - max_templates
        to_evict = [x[0] for x in template_timestamps[:num_to_evict]]
        
        for template in to_evict:
            if template in self.clusters:
                self.clusters.remove(template)
                
        # 2. Rebuild the tree with remaining templates
        self.root = Node(None)
        for template in self.clusters:
            self.insert(template)
