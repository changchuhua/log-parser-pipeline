import re
import signal

class RegexTimeoutException(Exception):
    """Exception raised when regex matching execution budget is exceeded."""
    pass

def regex_timeout_handler(signum, frame):
    """Signal handler raising RegexTimeoutException."""
    raise RegexTimeoutException("Regex matching execution timed out")

def template_to_regex(template):
    # Collapse runs of <*> placeholders into a single <*>, whether they're
    # space-separated ("<*> <*> <*>") or joined by a short literal delimiter
    # like a comma ("<*>,<*>,<*>" -- e.g. an LLM template that captured a JSON
    # array element-by-element instead of masking the whole array). Left
    # uncollapsed, a wide array like a 21-element SSL cipher list balloons into
    # dozens of separate (.*?) groups; matching that against a log line whose
    # shape doesn't align exactly forces the engine through an exponential
    # number of group-boundary combinations before it can fail -- reproduced
    # taking >10s on a single real botsv3 template (95 groups) that hung a live
    # run indefinitely. Collapsing here keeps the common case cheap; the
    # signal.alarm timeout guards at the call sites (parser.py) are the actual
    # backstop for whatever this collapse doesn't catch.
    collapsed = re.sub(r'<\*>\s*(?:<\*>\s*)+', '<*> ', template)
    collapsed = re.sub(r'<\*>(?:\s*[^\w\s<]{1,3}\s*<\*>)+', '<*>', collapsed).strip()
    escaped = re.escape(collapsed)
    # Support both unescaped and escaped angle brackets
    regex_str = escaped.replace(r'<\*>', r'(.*?)').replace(r'\<\*\>', r'(.*?)')
    return re.compile(f"^{regex_str}$")

def match_log(cache, log_message):
    log_tokens = log_message.split()
    old_handler = signal.signal(signal.SIGALRM, regex_timeout_handler)
    try:
        for entry in cache.cache:
            ref_tokens = entry["ref_log"].split()
            if len(log_tokens) == len(ref_tokens):
                try:
                    pattern = template_to_regex(entry["template"])
                    signal.alarm(1)
                    is_match = bool(pattern.match(log_message))
                except (RegexTimeoutException, Exception):
                    is_match = False
                finally:
                    signal.alarm(0)

                if is_match:
                    entry["frequency"] += 1
                    cache.sort_cache()
                    return entry["template"]
    finally:
        signal.signal(signal.SIGALRM, old_handler)
    return None
