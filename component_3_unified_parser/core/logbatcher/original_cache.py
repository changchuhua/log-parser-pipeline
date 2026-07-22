"""Faithful port of upstream LogBatcher's template-memory cache.

Ported from LogIntelligence/LogBatcher's logbatcher/parsing_cache.py --
a prefix-tree (trie) structural matcher with an O(1) SHA256-hash exact-match
fast path, distinct from this repo's own ParsingCache (OrderedDict-based LRU
+ Jaccard reconciliation, see parsing_cache.py). Selected via
config.yaml's logbatcher.cache_mode: "original".

add_templates()'s LCS-merge branch is ported verbatim but is dead code at
its only real call site in this repo (parser.py calls it the same way
upstream's parsing_base.py does: insert=False, relevant_templates left at
its default []), matching upstream's own actual runtime behavior --
see parser_implementation_comparison.md Section 1 for the full writeup.
"""

import re
import signal
import sys
from hashlib import sha256

sys.setrecursionlimit(1000000)


class TimeoutException(Exception):
    pass


def timeout_handler(signum, frame):
    raise TimeoutException()


def safe_search(pattern, string, timeout=1):
    """SIGALRM-bounded re.search -- same pattern already used safely in
    this codebase (see postprocess.py's match_and_prune)."""
    old_handler = signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(timeout)
    try:
        result = re.search(pattern, string)
    except TimeoutException:
        result = None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)
    return result


_PATTERN1 = re.compile(r'/([^/]*)(?=/)')  # path
_PATTERN2 = re.compile(r'\d')             # digit
_PATTERN3 = re.compile(r'[\/:,._-]+')     # : , . _ -
_PATTERN4 = re.compile(r'\s')             # space


def standardize(input_string: str) -> str:
    """Strips digits/paths/punctuation/whitespace for the hash exact-match fast path."""
    result = _PATTERN1.sub('', input_string)
    result = _PATTERN2.sub('', result)
    result = _PATTERN3.sub('', result)
    result = _PATTERN4.sub('', result)
    return result


def lcs_similarity(X, Y):
    m, n = len(X), len(Y)
    c = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if X[i - 1] == Y[j - 1]:
                c[i][j] = c[i - 1][j - 1] + 1
            else:
                c[i][j] = max(c[i][j - 1], c[i - 1][j])
    return 2 * c[m][n] / (m + n) if (m + n) > 0 else 0.0


def post_process_tokens(tokens, punc):
    excluded_str = ['=', '|', '(', ')', ";"]
    for i in range(len(tokens)):
        if tokens[i].find("<*>") != -1:
            tokens[i] = "<*>"
        else:
            new_str = ""
            for s in tokens[i]:
                if (s not in punc and s != ' ') or s in excluded_str:
                    new_str += s
            tokens[i] = new_str
    return tokens


def message_split(message):
    punc = "!\"#$%&'()+,-/;:=?@.[\\]^_`{|}~"
    splitters = "\\s\\" + "\\".join(punc)
    splitter_regex = re.compile("([{}])".format(splitters))
    tokens = re.split(splitter_regex, message)

    tokens = list(filter(lambda x: x != "", tokens))

    tokens = post_process_tokens(tokens, punc)

    tokens = [
        token.strip()
        for token in tokens
        if token != "" and token != ' '
    ]
    tokens = [
        token
        for idx, token in enumerate(tokens)
        if not (token == "<*>" and idx > 0 and tokens[idx - 1] == "<*>")
    ]
    return tokens


def match_log(log, template):
    pattern_parts = template.split("<*>")
    pattern_parts_escaped = [re.escape(part) for part in pattern_parts]
    regex_pattern = "(.*?)".join(pattern_parts_escaped)
    regex = "^" + regex_pattern + "$"
    matches = safe_search(regex, log)
    return matches is not None


def get_all_templates(move_tree):
    result = []
    for key, value in move_tree.items():
        if isinstance(value, tuple):
            result.append(value[2])
        else:
            result = result + get_all_templates(value)
    return result


def find_template(move_tree, log_tokens, result, parameter_list, depth):
    flag = 0  # no further find
    if len(log_tokens) == 0:
        for key, value in move_tree.items():
            if isinstance(value, tuple):
                result.append((key, value, tuple(parameter_list)))
                flag = 2  # match
        if "<*>" in move_tree:
            parameter_list.append("")
            sub_tree = move_tree["<*>"]
            if isinstance(sub_tree, tuple):
                result.append(("<*>", None, None))
                flag = 2  # match
            else:
                for key, value in sub_tree.items():
                    if isinstance(value, tuple):
                        result.append((key, value, tuple(parameter_list)))
                        flag = 2  # match
    else:
        token = log_tokens[0]

        relevant_templates = []
        if token in move_tree:
            find_result = find_template(move_tree[token], log_tokens[1:], result, parameter_list, depth + 1)
            if find_result[0]:
                flag = 2  # match
            elif flag != 2:
                flag = 1  # further find but no match
                relevant_templates = relevant_templates + find_result[1]
        if "<*>" in move_tree:
            if isinstance(move_tree["<*>"], dict):
                next_keys = move_tree["<*>"].keys()
                next_continue_keys = []
                for nk in next_keys:
                    nv = move_tree["<*>"][nk]
                    if not isinstance(nv, tuple):
                        next_continue_keys.append(nk)
                idx = 0
                while idx < len(log_tokens):
                    token = log_tokens[idx]
                    if token in next_continue_keys:
                        parameter_list.append("".join(log_tokens[0:idx]))
                        find_result = find_template(
                            move_tree["<*>"], log_tokens[idx:], result, parameter_list, depth + 1
                        )
                        if find_result[0]:
                            flag = 2  # match
                        elif flag != 2:
                            flag = 1  # further find but no match
                            relevant_templates = relevant_templates + find_result[1]
                        if parameter_list:
                            parameter_list.pop()
                        next_continue_keys.remove(token)
                    idx += 1
                if idx == len(log_tokens):
                    parameter_list.append("".join(log_tokens[0:idx]))
                    find_result = find_template(
                        move_tree["<*>"], log_tokens[idx + 1:], result, parameter_list, depth + 1
                    )
                    if find_result[0]:
                        flag = 2  # match
                    elif flag != 2:
                        flag = 1
                    if parameter_list:
                        parameter_list.pop()
    if flag == 2:
        return (True, [])
    if flag == 1:
        return (False, relevant_templates)
    if depth >= 2:
        return (False, get_all_templates(move_tree))
    return (False, [])


def match_template(match_tree, log_tokens):
    results = []
    find_results = find_template(match_tree, log_tokens, results, [], 1)
    relevant_templates = find_results[1]
    if len(results) > 1:
        new_results = [
            r for r in results
            if r[0] is not None and r[1] is not None and r[2] is not None
        ]
    else:
        new_results = results
    if new_results:
        if len(new_results) > 1:
            new_results.sort(key=lambda x: (-x[1][0], x[1][1]))
        return new_results[0][1][2], new_results[0][1][3], new_results[0][1][4], relevant_templates
    return False, False, '', relevant_templates


def tree_match(match_tree, template_list, log_content):
    log_tokens = message_split(log_content)
    template, template_id, refer_log, relevant_templates = match_template(match_tree, log_tokens)
    # length matters
    if template:
        if abs(len(log_content.split()) - len(refer_log.split())) <= 1:
            return (template, template_id, relevant_templates)
    elif relevant_templates:
        if match_log(log_content, relevant_templates[0]):
            return (relevant_templates[0], template_list.index(relevant_templates[0]), relevant_templates)
    return ("NoMatch", "NoMatch", relevant_templates)


class OriginalParsingCache:
    """Faithful port of upstream logbatcher.parsing_cache.ParsingCache."""

    def __init__(self):
        self.template_tree = {}
        self.template_list = []
        self.hashing_cache = {}
        self.variable_candidates = []
        self.hit_num = 0

    def add_templates(self, event_template, insert=True, relevant_templates=None, refer_log=''):
        if relevant_templates is None:
            relevant_templates = []

        template_tokens = message_split(event_template)
        if not template_tokens or event_template == "<*>":
            return -1, None, None
        if insert or len(relevant_templates) == 0:
            template_id = self.insert(event_template, template_tokens, len(self.template_list), refer_log)
            self.template_list.append(event_template)
            return template_id, None, None

        max_similarity = 0
        similar_template = None
        for rt in relevant_templates:
            splited_template1, splited_template2 = rt.split(), event_template.split()
            if len(splited_template1) != len(splited_template2):
                continue
            similarity = lcs_similarity(splited_template1, splited_template2)
            if similarity > max_similarity:
                max_similarity = similarity
                similar_template = rt
        if max_similarity > 0.8:
            success, template_id = self.modify(similar_template, event_template, refer_log)
            if not success:
                template_id = self.insert(event_template, template_tokens, len(self.template_list), refer_log)
                self.template_list.append(event_template)
            return template_id, similar_template, success
        else:
            template_id = self.insert(event_template, template_tokens, len(self.template_list), refer_log)
            self.template_list.append(event_template)
            return template_id, None, None

    def insert(self, event_template, template_tokens, template_id, refer_log=''):
        standardized = standardize(event_template)
        hash_key = sha256(standardized.encode()).hexdigest()
        self.hashing_cache[hash_key] = (standardized, event_template, template_id)

        start_token = template_tokens[0]
        if start_token not in self.template_tree:
            self.template_tree[start_token] = {}
        move_tree = self.template_tree[start_token]

        tidx = 1
        while tidx < len(template_tokens):
            token = template_tokens[tidx]
            if token not in move_tree:
                move_tree[token] = {}
            move_tree = move_tree[token]
            tidx += 1

        move_tree["".join(template_tokens)] = (
            sum(1 for s in template_tokens if s != "<*>"),
            template_tokens.count("<*>"),
            event_template,
            template_id,
            refer_log,
        )
        return template_id

    def modify(self, similar_template, event_template, refer_log):
        merged_template = []
        similar_tokens = similar_template.split()
        event_tokens = event_template.split()
        for i, token in enumerate(similar_tokens):
            if token == event_tokens[i]:
                merged_template.append(token)
            else:
                merged_template.append("<*>")
        merged_template = " ".join(merged_template)
        success, old_id = self.delete(similar_template)
        if not success:
            return False, -1
        self.insert(merged_template, message_split(merged_template), old_id, refer_log)
        self.template_list[old_id] = merged_template
        return True, old_id

    def delete(self, event_template):
        template_tokens = message_split(event_template)
        start_token = template_tokens[0]
        if start_token not in self.template_tree:
            return False, []
        move_tree = self.template_tree[start_token]

        tidx = 1
        while tidx < len(template_tokens):
            token = template_tokens[tidx]
            if token not in move_tree:
                return False, []
            move_tree = move_tree[token]
            tidx += 1
        old_id = move_tree["".join(template_tokens)][3]
        del move_tree["".join(template_tokens)]
        return True, old_id

    def match_event(self, log):
        standardized = standardize(log)
        hash_key = sha256(standardized.encode()).hexdigest()
        if hash_key in self.hashing_cache:
            cached_str, template, template_id = self.hashing_cache[hash_key]
            if cached_str == standardized:
                self.hit_num += 1
                return template, template_id, []
        results = tree_match(self.template_tree, self.template_list, log)
        if results[0] != "NoMatch":
            standardized = standardize(log)
            hash_key = sha256(standardized.encode()).hexdigest()
            self.hashing_cache[hash_key] = (standardized, results[0], results[1])
        return results
