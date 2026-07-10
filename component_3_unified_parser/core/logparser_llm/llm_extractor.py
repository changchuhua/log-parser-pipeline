"""In-Context Learning (ICL) Template Extractor for LogParser-LLM.

Computes log embeddings, extracts similar examples dynamically from a seed pool,
and queries the Ollama client to extract templates with semantic categories.
"""

import re
import json
import os
import logging
import yaml
from core.llm_client import OllamaClient

logger = logging.getLogger(__name__)

def get_jaccard_similarity(str1, str2):
    """Computes Jaccard Similarity between two strings on a token level."""
    tokens1 = set(str1.split())
    tokens2 = set(str2.split())
    if not tokens1 and not tokens2:
        return 1.0
    union_len = len(tokens1.union(tokens2))
    if union_len == 0:
        return 0.0
    return len(tokens1.intersection(tokens2)) / union_len

def get_variables_from_example(template, ref_log):
    """Automatically extracts dynamic variables matching tags in the template from ref_log."""
    tags = re.findall(r'<[A-Z]{3}>', template)
    if not tags:
        return []

    escaped_template = re.escape(template)
    pattern_str = escaped_template
    for tag in set(tags):
        pattern_str = pattern_str.replace(re.escape(tag), r'(.*?)')

    try:
        pattern = re.compile(f"^{pattern_str}$")
        match = pattern.match(ref_log)
        if match:
            variables = []
            for idx, val in enumerate(match.groups()):
                if idx < len(tags):
                    variables.append({
                        "category": tags[idx],
                        "value": val.strip()
                    })
            return variables
    except Exception:
        pass
    return []

class LLMExtractor:
    """Handles parsing logs using dynamic few-shot templates querying the LLM."""

    def __init__(self, tree_router, config_path='/app/config.yaml'):
        """Initializes LLMExtractor.

        Args:
            tree_router (PrefixTree): In-memory PrefixTree router references.
            config_path (str): YAML configuration path. Defaults to '/app/config.yaml'.
        """
        self.tree_router = tree_router
        self.llm_client = OllamaClient(config_path)
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}
        self.k_shots = config.get('logparser_llm', {}).get('k_shots', 3)
        self.template_pool = []

        # Load Template Pool from the existing LogBatcher cache file if available
        cache_file = '/app/data/cache/logbatcher_cache.json'
        if not os.path.exists(cache_file):
            cache_file = 'data/cache/logbatcher_cache.json'
        if os.path.exists(cache_file):
            try:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
                    for entry in entries:
                        if 'template' in entry and 'ref_log' in entry:
                            self.template_pool.append({
                                'template': entry['template'],
                                'ref_log': entry['ref_log']
                            })
            except Exception as e:
                logger.error(f"Error loading template pool for ICL: {e}")

        # Fallback seed examples if template pool is empty to ensure always functioning shot retrieval
        if not self.template_pool:
            self.template_pool = [
                {"template": "User <LOI> logged in", "ref_log": "User admin logged in"},
                {"template": "mice: PS/2 mouse device common for all mice", "ref_log": "mice: PS/2 mouse device common for all mice"},
                {"template": "bindcache: failed init IPS: <TDA> (<OID>)", "ref_log": "bindcache: failed init IPS: 0x5 (Out of memory)"}
            ]

    def get_template(self, log_message, log_record=None):
        """Retrieves template and extracts ECS fields using dynamic Jaccard-based K-shot prompting.

        Args:
            log_message (str): Raw log message content.
            log_record (dict, optional): Reference log record dict to enrich with ECS fields.

        Returns:
            str: Evaluated static template.
        """
        # Retrieve top K based on Jaccard similarity of logs
        candidates = []
        for entry in self.template_pool:
            sim = get_jaccard_similarity(log_message, entry['ref_log'])
            candidates.append((sim, entry['ref_log'], entry['template']))
        candidates.sort(key=lambda x: x[0], reverse=True)
        top_k = candidates[:self.k_shots]

        demonstrations = ""
        for sim, ref_log, template in top_k:
            variables = get_variables_from_example(template, ref_log)
            ex_json = {
                "template": template,
                "variables": variables
            }
            demonstrations += f"Log: {ref_log}\nOutput: {json.dumps(ex_json)}\n\n"

        sys_prompt = (
            "As a log parser, your task is to analyze logs and identify dynamic variables. "
            "The allowed semantic categories are: Location Indicator (<LOI>), Object ID (<OID>), and Time/Date/Activity (<TDA>). "
            "You MUST output a valid JSON object containing the normalized template string "
            "where variables are replaced by their category tokens, and a list of extracted variables.\n"
            "CRITICAL: Do NOT include any markdown code blocks, introductory text, conversational preamble, or explanation. Output ONLY the raw JSON object."
        )

        if demonstrations:
            sys_prompt += f"\n\nExamples:\n{demonstrations}"

        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"Log: {log_message}\nOutput:"}
        ]

        ECS_MAPPING = {
            "<LOI>": "source.ip",
            "<OID>": "file.path",
            "<TDA>": "event.ingested"
        }

        try:
            response = self.llm_client.generate_completion(messages).strip()
            # Clean markdown code blocks if the LLM output wrapped them
            if response.startswith("```"):
                lines = response.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines[-1].startswith("```"):
                    lines = lines[:-1]
                response = "\n".join(lines).strip()

            try:
                parsed_json = json.loads(response)
                if isinstance(parsed_json, dict):
                    template = parsed_json.get("template", log_message)

                    # Map categories to ECS fields directly on the log record object
                    if log_record is not None and "variables" in parsed_json and isinstance(parsed_json["variables"], list):
                        for var in parsed_json["variables"]:
                            if isinstance(var, dict):
                                cat = var.get("category")
                                val = var.get("value")
                                if cat in ECS_MAPPING and val:
                                    ecs_field = ECS_MAPPING[cat]
                                    log_record[ecs_field] = val
                else:
                    template = response
            except Exception as json_err:
                logger.info(f"LLM output was not valid JSON ({json_err}). Treating raw output as template string.")
                template = response

            # Update PrefixTree Router
            self.tree_router.insert(template)
            # Add to template pool
            self.template_pool.append({
                'template': template,
                'ref_log': log_message
            })
            return template

        except Exception as e:
            logger.error(f"LLM generation/fallback error: {e}. Falling back to literal.")
            self.tree_router.insert(log_message)
            return log_message
