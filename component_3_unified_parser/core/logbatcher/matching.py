import re

def template_to_regex(template):
    escaped = re.escape(template)
    regex_str = escaped.replace(r'\<\*\>', r'(.*?)')
    return re.compile(f"^{regex_str}$")

def match_log(cache, log_message):
    log_tokens = log_message.split()
    for entry in cache.cache:
        ref_tokens = entry["ref_log"].split()
        if len(log_tokens) == len(ref_tokens):
            pattern = template_to_regex(entry["template"])
            if pattern.match(log_message):
                entry["frequency"] += 1
                cache.sort_cache()
                return entry["template"]
    return None
