"""Post-processing and regex matching engine for LogBatcher.

Implements clean_template parsing of LLM output and the Match & Prune loop.
"""

import re
from .matching import template_to_regex

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

    for log in partition:
        msg = log.get('message', '')
        if pattern.match(msg):
            matched_logs.append(log)
        else:
            pruned_logs.append(log)

    if matched_logs:
        cache.add(cleaned_tmpl, matched_logs[0]['message'])

    return matched_logs, pruned_logs
