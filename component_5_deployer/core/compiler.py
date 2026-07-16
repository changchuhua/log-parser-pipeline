import json
import re

class IngestPipelineCompiler:
    """Translates ECS log templates with placeholder tags into a valid ES Ingest Pipeline JSON."""

    # Map placeholder tags (raw, unescaped form) to standard Grok regex abstractions.
    # Field names mirror the ECS field each tag maps to in llm_extractor.py's
    # ECS_MAPPING (paper_10 and ecs_10 categories_mode), so the same variable
    # concept gets the same field name whether it was injected directly by the
    # LLM extractor or extracted downstream by this Grok pipeline.
    #
    # Exceptions, both deliberate:
    # - <TDA> targets a plain "timestamp" field here, not ECS_MAPPING's
    #   "event.ingested". Grok field names with dots are written as real nested
    #   ECS paths by Elasticsearch (unlike llm_extractor.py's flat JSONL dict
    #   key of the same name, which never reaches ES). event.ingested has a
    #   fixed ECS meaning -- arrival time at the pipeline -- which is not what
    #   <TDA> extracts (a timestamp parsed from the log's own text). Reusing it
    #   here would let this pipeline overwrite that standard field with the
    #   wrong value on every real document it processes.
    # - <OTP> targets "otp_value", not ECS_MAPPING's "message", to avoid
    #   colliding with the grok processor's own source field below.
    TAG_TO_GROK = {
        # Shared across categories_mode: paper_10, ecs_10, ecs_3
        "<LOI>": "%{IP:source.ip}",
        "<TDA>": "%{TIMESTAMP_ISO8601:timestamp}",
        "<OID>": "%{NOTSPACE:file.path}",
        # paper_10-only tags
        "<OBN>": "%{NOTSPACE:process.name}",
        "<TID>": "%{NOTSPACE:event.type}",
        "<SID>": "%{NOTSPACE:event.action}",
        "<CRS>": "%{NOTSPACE:host.cpu}",
        "<OBA>": "%{NUMBER:event.duration}",
        "<STC>": "%{NOTSPACE:event.outcome}",
        "<OTP>": "%{GREEDYDATA:otp_value}",
        # ecs_10-only tags
        "<USR>": "%{NOTSPACE:user.name}",
        "<POR>": "%{NUMBER:source.port}",
        "<STA>": "%{NOTSPACE:event.outcome}",
        "<VER>": "%{NOTSPACE:service.version}",
        "<PRO>": "%{WORD:network.transport}",
        "<NUM>": "%{NUMBER:event.duration}",
        "<COM>": "%{NOTSPACE:process.name}",
        # LogParser-LLM's untyped wildcard
        "<*>": "%{GREEDYDATA:custom_field}",
    }

    @staticmethod
    def compile_template_to_grok(template_str: str) -> str:
        """Escapes raw log characters first, then replaces placeholders with Grok macros.

        Tags are swapped for sentinel markers *before* escaping, then the
        markers are swapped for their Grok macros after. Matching tags against
        the already-escaped string (the previous approach) was fragile: Python's
        re.escape() does not treat `<`/`>` as special characters, so escaped
        tags never matched their expected `\\<TAG\\>` keys and no substitution
        ever fired.
        """
        # 1. Replace tags with unique sentinel markers (characters re.escape() won't touch).
        working = template_str
        sentinels = {}
        for idx, tag in enumerate(IngestPipelineCompiler.TAG_TO_GROK):
            marker = f"@@GROKTAG{idx}@@"
            sentinels[marker] = IngestPipelineCompiler.TAG_TO_GROK[tag]
            working = working.replace(tag, marker)

        # 2. Escape all raw regex characters (e.g. [, ], ?, *, .) in the remaining literal text.
        escaped_pattern = re.escape(working)

        # 3. Replace sentinel markers with their Grok macros.
        for marker, grok_expr in sentinels.items():
            escaped_pattern = escaped_pattern.replace(marker, grok_expr)

        # 4. Clean up escapes that might break the Grok parser.
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
