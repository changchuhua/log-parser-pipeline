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

def jaccard_token_similarity(t1, t2):
    """Length-independent token-set Jaccard similarity, used as a cheap pre-filter
    before any LLM merge-check call (see TemplateManager._calibrate_llm)."""
    tokens1 = set(t1.split(' '))
    tokens2 = set(t2.split(' '))
    if not tokens1 or not tokens2:
        return 0.0
    union_len = len(tokens1 | tokens2)
    if union_len == 0:
        return 0.0
    return len(tokens1 & tokens2) / union_len

class TemplateManager:
    """Manages merging and calibration of PrefixTree templates."""

    def __init__(self, tree_router, config_path='/app/config.yaml', llm_client=None):
        """Initializes TemplateManager.

        Args:
            tree_router (PrefixTree): Targets tree router references.
            config_path (str): YAML configuration path. Defaults to '/app/config.yaml'.
            llm_client (OllamaClient, optional): Only required when merge_mode ==
                "original" -- shares LLMExtractor's client rather than opening a
                second one.
        """
        self.tree_router = tree_router
        self.llm_client = llm_client
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}
        lp_config = config.get('logparser_llm', {})
        self.merge_similarity_threshold = lp_config.get('merge_similarity_threshold', 0.95)
        self.merge_mode = lp_config.get('merge_mode', 'production')
        self.merge_prefilter_threshold = lp_config.get('merge_prefilter_threshold', 0.3)

    def calibrate(self):
        """Merges overlapping clusters in PrefixTree.

        merge_mode == "production" (default): purely structural, same-token-count
        positional comparison, mismatched positions substituted with <*>. No LLM.

        merge_mode == "original": LLM-driven semantic merge decision, faithful in
        *spirit* to the paper's Figure 6/7 prompts (check, then verify-and-unify) --
        see _calibrate_llm()'s docstring for the specific adaptations this required.
        """
        if self.merge_mode == 'original':
            self._calibrate_llm()
        else:
            self._calibrate_structural()

    def try_merge_pair(self, template_a, template_b):
        """Attempts to merge exactly two templates, using whichever mechanism
        merge_mode selects. Returns the unified template string, or None if they
        shouldn't merge. Used both by calibrate()'s periodic pairwise pass and by
        match_llm_mode="original"'s inline merge-check against loose-matched
        candidates in main_parser.py (see LogParser-LLM's per-log loop)."""
        if self.merge_mode == 'original':
            if jaccard_token_similarity(template_a, template_b) < self.merge_prefilter_threshold:
                return None
            if not self._llm_merge_check(template_a, template_b):
                return None
            return self._llm_merge_verify(template_a, template_b)

        tokens_a = template_a.split(' ')
        tokens_b = template_b.split(' ')
        if len(tokens_a) != len(tokens_b):
            return None
        if string_structural_similarity(tokens_a, tokens_b) < self.merge_similarity_threshold:
            return None
        merged_tokens = list(tokens_a)
        for k in range(len(merged_tokens)):
            if merged_tokens[k] != tokens_b[k]:
                merged_tokens[k] = '<*>'
        return ' '.join(merged_tokens)

    def _calibrate_structural(self):
        """merge_mode == "production": original structural calibrate() logic, unchanged."""
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

    def _calibrate_llm(self):
        """merge_mode == "original": LLM-driven merge, adapted from the paper's Figure 6
        ("Merge Verification") and Figure 7 ("Merge Checking") prompts.

        Two disclosed adaptations from the paper's literal design, both forced by what
        data is actually available at this point in the pipeline:

        1. Figure 7's prompt expects real log instances ("does this template apply to
           the following logs?"), but PrefixTree/TemplateManager only retain template
           *strings* per cluster -- no raw log history survives per leaf node. The
           check/verify prompts here compare the two TEMPLATE STRINGS directly instead
           ("do these represent the same underlying event?"), not literal logs.
        2. Unlike merge_mode="production" (and unlike calibrate()'s own pairing loop,
           which only ever compares same-token-count pairs), this path is NOT
           restricted to equal-length templates -- candidate pairs are pre-filtered by
           length-independent Jaccard token overlap instead, and the unified template
           comes directly from the LLM's own generation rather than a positional zip
           (which requires equal length). This closes the concrete, documented gap in
           the structural approach: it can never even compare two templates whose
           token counts differ, regardless of how similar they otherwise are.

        Scale guard: an LLM call pair (check, then verify only if check says yes) is
        only spent on pairs that already clear merge_prefilter_threshold on the cheap
        Jaccard pre-filter -- not literally every pair, which would be far too
        expensive at the ~1,000-template scale prune_to_capacity allows. Same bounding
        principle as LogBatcher's historical_variables_cap.
        """
        clusters = list(self.tree_router.clusters)
        merged = []
        skip_indices = set()

        for i in range(len(clusters)):
            if i in skip_indices:
                continue
            current = clusters[i]
            for j in range(i + 1, len(clusters)):
                if j in skip_indices:
                    continue
                candidate = clusters[j]
                if jaccard_token_similarity(current, candidate) < self.merge_prefilter_threshold:
                    continue
                if self._llm_merge_check(current, candidate):
                    unified = self._llm_merge_verify(current, candidate)
                    if unified:
                        current = unified
                        skip_indices.add(j)
            merged.append(current)

        unique_merged = list(set(merged))
        self.tree_router.clusters = []
        self.tree_router.root.children = {}
        for tmpl in unique_merged:
            self.tree_router.insert(tmpl)

    def _llm_merge_check(self, template_a, template_b):
        """Figure 7, adapted to compare two template strings instead of logs-vs-template.
        Binary yes/no: do these represent the same underlying log event?"""
        if self.llm_client is None:
            return False
        messages = [
            {"role": "system", "content": (
                "You are checking whether two log parsing templates represent the same "
                "underlying log event, just with different variable boundaries identified. "
                "Answer with only \"yes\" or \"no\"."
            )},
            {"role": "user", "content": f"Template A: {template_a}\nTemplate B: {template_b}\nAnswer:"}
        ]
        try:
            response = self.llm_client.generate_completion(messages).strip().lower()
        except Exception:
            return False
        return response.startswith('yes')

    def _llm_merge_verify(self, template_a, template_b):
        """Figure 6, adapted: produce a single unified template generalizing both
        inputs, or None if they turn out not to represent the same event after all."""
        if self.llm_client is None:
            return None
        messages = [
            {"role": "system", "content": (
                "Two log parsing templates were flagged as possibly representing the same "
                "underlying log event. If they do represent the same event, produce a single "
                "unified template that generalizes both: replace any token that differs "
                "between them, or that either template already marked as a variable (a "
                "<TAG>-style placeholder), with <*>. Keep all other identical, static tokens "
                "unchanged. If they do NOT represent the same event, respond with exactly: "
                "None. Output ONLY the unified template or the word None -- no explanation, "
                "no markdown."
            )},
            {"role": "user", "content": f"Template A: {template_a}\nTemplate B: {template_b}\nUnified Template:"}
        ]
        try:
            response = self.llm_client.generate_completion(messages).strip()
        except Exception:
            return None
        if response.startswith("```"):
            lines = response.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            response = "\n".join(lines).strip()
        if not response or response.lower() == 'none':
            return None
        return response
