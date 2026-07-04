import re
from .matching import template_to_regex

def clean_template(llm_output):
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
    cleaned_tmpl = clean_template(template)
    matched_logs = []
    pruned_logs = []

    try:
        pattern = template_to_regex(cleaned_tmpl)
    except Exception as e:
        return [], partition

    for log in partition:
        msg = log.get('message', '')
        if pattern.match(msg):
            matched_logs.append(log)
        else:
            pruned_logs.append(log)

    if matched_logs:
        cache.add(cleaned_tmpl, matched_logs[0]['message'])

    return matched_logs, pruned_logs
