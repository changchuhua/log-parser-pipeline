"""Wires a target ingest pipeline (default: so_custom_ingest_pipeline) into
Security Onion's global@custom pipeline, so new incoming logs that the
standard pipeline leaves uncategorized get routed through it automatically.

Deliberately a separate script from main_deployer.py, not folded into its
default flow -- global@custom is a Security-Onion-owned, cluster-wide
resource (runs on nearly every document across nearly every data stream),
a materially larger blast radius than the isolated so_custom_ingest_pipeline
main_deployer.py manages. Run this explicitly, once, rather than on every
deploy.

Usage:
    docker compose run --rm component_5 python wire_global_custom.py
"""
import os
import json
import sys
import yaml
from dotenv import load_dotenv
from core.es_client import ElasticsearchDeployer
from core.validator import IngestPipelineValidator
from core.salt_sftp import SaltstackDeployer
from core.global_custom_wirer import build_wired_pipeline, GLOBAL_CUSTOM_PIPELINE_NAME

DEFAULT_CONDITION = "ctx.event?.category == null"


def main():
    load_dotenv()

    required_envs = ["SO_IP", "SO_USER", "SO_PASS", "TAILSCALE_NODE"]
    missing_envs = [env for env in required_envs if not os.environ.get(env)]
    if missing_envs:
        print(f"Error: Missing required environment variables: {', '.join(missing_envs)}. Aborting.", file=sys.stderr)
        sys.exit(1)

    with open("/app/config.yaml", "r") as f:
        config = yaml.safe_load(f)

    deployer_config = config["deployer"]
    dry_run = deployer_config["dry_run"]
    gc_config = deployer_config.get("global_custom") or {}
    target_pipeline = gc_config.get("target_pipeline") or deployer_config["pipeline_name"]
    condition = gc_config.get("condition") or DEFAULT_CONDITION

    es_deployer = ElasticsearchDeployer(deployer_config)

    # 1. Fetch global@custom's current definition -- never assume/hardcode a
    # baseline. It's Security-Onion-owned; this script only ever appends.
    print(f"Fetching current '{GLOBAL_CUSTOM_PIPELINE_NAME}' pipeline from Elasticsearch...")
    try:
        current = es_deployer.get_deployed_pipeline(GLOBAL_CUSTOM_PIPELINE_NAME)
    except Exception as e:
        print(f"Failed to fetch '{GLOBAL_CUSTOM_PIPELINE_NAME}': {e}", file=sys.stderr)
        sys.exit(1)

    if not current or GLOBAL_CUSTOM_PIPELINE_NAME not in current:
        print(
            f"Error: '{GLOBAL_CUSTOM_PIPELINE_NAME}' does not exist on this cluster. "
            "Refusing to create it from scratch -- its baseline processors are "
            "Security-Onion-owned and this script has no business inventing them. "
            "Investigate why it's missing before proceeding.",
            file=sys.stderr,
        )
        sys.exit(1)

    current_body = current[GLOBAL_CUSTOM_PIPELINE_NAME]

    # 2. Idempotent merge
    merged_body, changed = build_wired_pipeline(current_body, target_pipeline, condition)

    if not changed:
        print(f"'{GLOBAL_CUSTOM_PIPELINE_NAME}' already routes to '{target_pipeline}'. Nothing to do.")
        sys.exit(0)

    print(f"Adding a processor to '{GLOBAL_CUSTOM_PIPELINE_NAME}' routing unmapped logs to '{target_pipeline}'.")
    print(f"Condition: {condition}")

    # 3. Pre-flight simulation -- catch syntax errors before touching the live
    # pipeline. Synthetic doc deliberately has no event.category, so the
    # condition is exercised the same way a real unmapped log would hit it.
    print("Running pre-flight simulation on Elasticsearch...")
    validator = IngestPipelineValidator(deployer_config)
    try:
        validator.simulate_pipeline(merged_body, "synthetic test log for global@custom pre-flight simulation")
        print("Pre-flight Simulation Successful.")
    except Exception as e:
        print(f"Pre-flight Simulation Failed: {e}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"[DRY-RUN] Would deploy the following merged '{GLOBAL_CUSTOM_PIPELINE_NAME}' pipeline:")
        print(json.dumps(merged_body, indent=2))
        sys.exit(0)

    # 4. Two-pronged deployment, same pattern as main_deployer.py
    # Prong A: immediate PUT
    try:
        es_deployer.deploy(GLOBAL_CUSTOM_PIPELINE_NAME, merged_body)
        print(f"Step A: Immediate '{GLOBAL_CUSTOM_PIPELINE_NAME}' PUT to Elasticsearch successful.")
    except Exception as e:
        print(f"Step A failed: {e}", file=sys.stderr)
        sys.exit(1)

    # Prong B: persistent SaltStack transfer, exact filename (no ".json"
    # suffix) -- so-elasticsearch-pipelines uses the filename itself as the
    # pipeline name on every highstate, so this is what makes the change
    # survive one, unlike a bare API PUT.
    temp_file = "/app/data/parsed/global_custom_merged.json"
    try:
        with open(temp_file, "w", encoding="utf-8") as tf:
            json.dump(merged_body, tf, indent=4)
        salt_deployer = SaltstackDeployer(deployer_config)
        salt_deployer.deploy_persistently_exact(GLOBAL_CUSTOM_PIPELINE_NAME, temp_file)
        print(f"Step B: Persistent '{GLOBAL_CUSTOM_PIPELINE_NAME}' configuration copied to Saltstack successfully.")
    except Exception as e:
        print(f"Step B failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
