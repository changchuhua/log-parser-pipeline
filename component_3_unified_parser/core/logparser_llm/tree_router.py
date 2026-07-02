import yaml

def jaccard_similarity(tokens1, tokens2):
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2:
        return 1.0
    return len(set1.intersection(set2)) / len(set1.union(set2))

class Node:
    def __init__(self, token):
        self.token = token
        self.children = {}
        self.cluster = None

class PrefixTree:
    def __init__(self, config_path='/app/config.yaml'):
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
        self.loose_match_threshold = config.get('logparser_llm', {}).get('loose_match_threshold', 0.8)
        self.root = Node(None)
        self.clusters = []  # List of templates
        
    def strict_match(self, tokens):
        # Workflow Phase 1 (Strict Match)
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
        # Workflow Phase 2 (Loose Match)
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
