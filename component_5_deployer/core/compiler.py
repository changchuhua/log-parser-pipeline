import json
import re

class IngestPipelineCompiler:
    """Translates ECS log templates with placeholder tags into a valid ES Ingest Pipeline JSON."""
    
    # Map placeholder tags (escaped form) to standard Grok regex abstractions
    TAG_TO_GROK = {
        r"\<LOI\>": "%{IP:source.ip}",
        r"\<TDA\>": "%{TIMESTAMP_ISO8601:timestamp}",
        r"\<OID\>": "%{NOTSPACE:file.path}",
        r"\<\*\>": "%{GREEDYDATA:custom_field}"
    }
    
    @staticmethod
    def compile_template_to_grok(template_str: str) -> str:
        """Escapes raw log characters first, then replaces placeholders with Grok macros."""
        # 1. Escape all raw regex characters (e.g. [, ], ?, *, .)
        escaped_pattern = re.escape(template_str)
        
        # 2. Replace escaped placeholder tags with Grok macros
        for tag_escaped, grok_expr in IngestPipelineCompiler.TAG_TO_GROK.items():
            escaped_pattern = escaped_pattern.replace(tag_escaped, grok_expr)
            
        # 3. Clean up double escapes that might break Grok parser
        escaped_pattern = (
            escaped_pattern
            .replace(r"\ ", " ")
            .replace(r"\_", "_")
            .replace(r"\-", "-")
        )
            
        return escaped_pattern
        
    def build_pipeline_json(self, parsed_logs_path: str, pipeline_name: str) -> dict:
        """Reads parsed templates and compiles them into a complete ingest pipeline JSON with failure tagging."""
        patterns = []
        with open(parsed_logs_path, "r", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line.strip())
                template = record.get("parsed_template")
                if template:
                    grok_pattern = self.compile_template_to_grok(template)
                    if grok_pattern not in patterns:
                        patterns.append(grok_pattern)
                        
        pipeline_json = {
            "description": f"Custom LLM-compiled Grok ingest pipeline '{pipeline_name}'",
            "processors": [
                {
                    "grok": {
                        "field": "message",
                        "patterns": patterns,
                        "ignore_missing": True,
                        "on_failure": [
                            {
                                "append": {
                                    "field": "tags",
                                    "value": ["_llm_grok_parse_failure"]
                                }
                            }
                        ]
                    }
                }
            ]
        }
        return pipeline_json
