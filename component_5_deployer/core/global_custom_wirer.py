GLOBAL_CUSTOM_PIPELINE_NAME = "global@custom"


def build_wired_pipeline(current_pipeline_body: dict, target_pipeline: str, condition: str) -> tuple[dict, bool]:
    """Given global@custom's current definition, returns (merged_body, changed).

    Deliberately additive, never a wholesale rewrite: reads whatever
    processors Security Onion currently ships in global@custom and appends
    to them, rather than hardcoding a known baseline. A hardcoded copy of
    "SO's N processors" would silently revert any future SO update to this
    file on the next deploy.

    changed=False (idempotent no-op) if a `pipeline` processor already
    routes to target_pipeline -- callers should skip the PUT/SFTP steps
    entirely in that case, not just detect it.
    """
    processors = list(current_pipeline_body.get("processors", []))
    already_wired = any(
        isinstance(p, dict) and p.get("pipeline", {}).get("name") == target_pipeline
        for p in processors
    )
    if already_wired:
        return current_pipeline_body, False

    new_processor = {
        "pipeline": {
            "name": target_pipeline,
            "if": condition,
            "ignore_missing_pipeline": True,
            "description": f"Route logs the standard pipeline left unmapped through {target_pipeline}",
        }
    }

    merged = dict(current_pipeline_body)
    merged["processors"] = processors + [new_processor]
    # "version" is response-only metadata from GET /_ingest/pipeline -- not a
    # valid field in a PUT body (mirrors the same strip in main_deployer.py's
    # idempotency-check re-PUT path).
    merged.pop("version", None)
    return merged, True


def remove_wired_pipeline(current_pipeline_body: dict, target_pipeline: str) -> tuple[dict, bool]:
    """Inverse of build_wired_pipeline(): strips any `pipeline` processor
    routing to target_pipeline out of global@custom's current definition.

    changed=False (idempotent no-op) if no such processor is present --
    callers should skip the PUT/SFTP steps entirely in that case.
    """
    processors = list(current_pipeline_body.get("processors", []))
    kept = [
        p for p in processors
        if not (isinstance(p, dict) and p.get("pipeline", {}).get("name") == target_pipeline)
    ]
    if len(kept) == len(processors):
        return current_pipeline_body, False

    reverted = dict(current_pipeline_body)
    reverted["processors"] = kept
    reverted.pop("version", None)
    return reverted, True
