import re
import signal
import string
from .matching import template_to_regex, RegexTimeoutException, regex_timeout_handler


def exclude_digits(token):
    """Faithful port of upstream LogBatcher's postprocess.py::exclude_digits().

    Flags a token as digit-dominated (should become <*>): alpha-leading or
    any-uppercase tokens are never flagged; otherwise flagged if it has 4+
    digit characters, or digits make up more than 30% of the token.
    """
    digits = re.findall(r'\d', token)
    if len(digits) == 0 or token[0].isalpha() or any(c.isupper() for c in token):
        return False
    elif len(digits) >= 4:
        return True
    else:
        return len(digits) / len(token) > 0.3


def verify_template(template):
    """Faithful port of upstream LogBatcher's util.py::verify_template().

    Rejects a degenerate template: False if, after removing <*> and spaces,
    nothing but punctuation remains.
    """
    stripped = template.replace("<*>", "").replace(" ", "")
    return any(char not in string.punctuation for char in stripped)


def correct_single_template(template, user_strings=None):
    """Faithful port of upstream LogBatcher's postprocess.py::correct_single_template().

    Multi-rule template normalization cascade applied to an already <*>-tagged
    template (or a raw log, as the verify_template() fallback path does):
    DS (double space), PS (path-like string collapsing), BL/US (boolean and
    default-string substitution), DG (digit-heavy token detection, via
    exclude_digits), WV (word concatenated with variable), DV (dot-separated
    consecutive variables), CV (consecutive variables), plus a battery of
    delimiter-specific <*> collapsing rules and size-unit collapsing.
    """
    boolean = {'true', 'false'}
    default_strings = {'null', 'root'}
    path_delimiters = {
        r'\s', r'\,', r'\!', r'\;', r'\:',
        r'\=', r'\|', r'\"', r"\'", r'\+',
        r'\[', r'\]', r'\(', r'\)', r'\{', r'\}'
    }
    token_delimiters = path_delimiters.union({
        r'\.', r'\-', r'\@', r'\#', r'\$', r'\%', r'\&', r'\/'
    })

    if user_strings:
        default_strings = default_strings.union(user_strings)

    template = template.strip()
    template = re.sub(r'\s+', ' ', template)

    # PS: path-like string collapsing
    p_tokens = re.split('(' + '|'.join(path_delimiters) + ')', template)
    new_p_tokens = []
    for p_token in p_tokens:
        if re.match(r'^(\/[^\/]+)+\/?$', p_token) or re.match(r'.*/.*\..*', p_token) or re.match(r'^([a-zA-Z0-9-]+\.){3,}[a-z]+$', p_token):
            p_token = '<*>'
        new_p_tokens.append(p_token)
    template = ''.join(new_p_tokens)

    tokens = re.split('(' + '|'.join(token_delimiters) + ')', template)
    new_tokens = []
    for token in tokens:
        for to_replace in boolean.union(default_strings):
            if token == to_replace:
                token = '<*>'
        if exclude_digits(token):
            token = '<*>'
        if re.match(r'^[^\s\/]*<\*>[^\s\/]*$', token) or re.match(r'^<\*>.*<\*>$', token):
            token = '<*>'
        new_tokens.append(token)
    template = ''.join(new_tokens)

    while True:
        prev = template
        template = re.sub(r'<\*>\.<\*>', '<*>', template)
        if prev == template:
            break

    while True:
        prev = template
        template = re.sub(r'<\*><\*>', '<*>', template)
        if prev == template:
            break

    for pattern, replacement in [
        ("#<*>#", "<*>"), ("<*>:<*>", "<*>"), ("<*>/<*>", "<*>"),
        (" #<*> ", " <*> "), ("<*>#<*>", "<*>"), ("<*>@<*>", "<*>"),
        ("<*>.<*>", "<*>"), (' "<*>" ', ' <*> '), (" '<*>' ", " <*> "),
        ("<*><*>", "<*>"),
    ]:
        while pattern in template:
            template = template.replace(pattern, replacement)

    template = re.sub(r'<\*> [KGTM]?B\b', '<*>', template)

    return template


def apply_original_postprocessing(raw_template, fallback_log_message):
    """Faithful port of upstream's post_process()-tail + verify_template() gate
    (get_responce()'s validation flow): runs correct_single_template()'s full
    normalization cascade, then rejects a degenerate result (nothing but
    <*>/punctuation) in favor of a rule-only pass on the raw log, no LLM.
    """
    template = correct_single_template(raw_template)
    if not verify_template(template):
        template = correct_single_template(fallback_log_message)
    return template


def clean_template(llm_output):
    """Cleans up raw markdown or backticks from LLM output.

    Args:
        llm_output (str): Raw template string from LLM.

    Returns:
        str: Sanitized template.
    """
    if not llm_output:
        return ""
    template = llm_output.strip()
    if template.startswith("```"):
        template = template.split('\n', 1)[-1]
        if template.endswith("```"):
            template = template.rsplit('\n', 1)[0]
    template = template.replace('`', '').strip()
    return template

def match_and_prune(template, partition, cache):
    """Matches partition logs using a compiled regex from the template.

    Updates cache frequencies with matched values, returning matched records and
    pruned residuals.

    Args:
        template (str): Candidate parsing template.
        partition (list): List of logs in the current cluster partition.
        cache (ParsingCache): Template frequency-cache instance.

    Returns:
        tuple: (matched_logs list, pruned_logs list).
    """
    cleaned_tmpl = clean_template(template)
    matched_logs = []
    pruned_logs = []

    try:
        pattern = template_to_regex(cleaned_tmpl)
    except Exception as e:
        return [], partition

    old_handler = signal.signal(signal.SIGALRM, regex_timeout_handler)
    try:
        for log in partition:
            msg = log.get('message', '')
            try:
                signal.alarm(1)
                is_match = bool(pattern.match(msg))
            except (RegexTimeoutException, Exception):
                is_match = False
            finally:
                signal.alarm(0)

            if is_match:
                matched_logs.append(log)
            else:
                pruned_logs.append(log)
    finally:
        signal.signal(signal.SIGALRM, old_handler)

    if matched_logs:
        cache.add(cleaned_tmpl, matched_logs[0]['message'])

    return matched_logs, pruned_logs
