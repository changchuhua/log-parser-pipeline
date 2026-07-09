import re
import signal

class RegexTimeoutException(Exception):
    """Exception raised when regex matching execution budget is exceeded."""
    pass

def regex_timeout_handler(signum, frame):
    """Signal handler raising RegexTimeoutException."""
    raise RegexTimeoutException("Regex matching execution timed out")

def template_to_regex(template):
    # Collapse multiple consecutive/space-separated <*> placeholders into a single <*>
    # to prevent catastrophic regex backtracking (e.g. "<*> <*> <*>" -> "<*>")
    collapsed = re.sub(r'<\*>\s*(?:<\*>\s*)+', '<*> ', template).strip()
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
