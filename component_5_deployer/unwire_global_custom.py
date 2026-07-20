"""Reverts wire_global_custom.py: strips the `pipeline` processor routing
into the target pipeline (default: so_custom_ingest_pipeline) back out of
Security Onion's global@custom pipeline.

Purely subtractive, mirrors wire_global_custom.py's safety pattern exactly
(idempotent, pre-flight simulate, dry_run gate, two-pronged deploy). Never
touches any other processor in global@custom -- if Security Onion's own
baseline changed since the wire, this only removes the one processor this
tooling added, leaving everything else as currently deployed.

Usage:
    docker compose run --rm component_5 python unwire_global_custom.py
"""
import os
import json
import sys
import yaml
from dotenv import load_dotenv
from core.es_client import ElasticsearchDeployer
from core.validator import IngestPipelineValidator
from core.salt_sftp import SaltstackDeployer
from core.global_custom_wirer import remove_wired_pipeline, GLOBAL_CUSTOM_PIPELINE_NAME

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

    es_deployer = ElasticsearchDeployer(deployer_config)

    print(f"Fetching current '{GLOBAL_CUSTOM_PIPELINE_NAME}' pipeline from Elasticsearch...")
    try:
        current = es_deployer.get_deployed_pipeline(GLOBAL_CUSTOM_PIPELINE_NAME)
    except Exception as e:
        print(f"Failed to fetch '{GLOBAL_CUSTOM_PIPELINE_NAME}': {e}", file=sys.stderr)
        sys.exit(1)

    if not current or GLOBAL_CUSTOM_PIPELINE_NAME not in current:
        print(f"Error: '{GLOBAL_CUSTOM_PIPELINE_NAME}' does not exist on this cluster. Nothing to revert.", file=sys.stderr)
        sys.exit(1)

    current_body = current[GLOBAL_CUSTOM_PIPELINE_NAME]

    reverted_body, changed = remove_wired_pipeline(current_body, target_pipeline)

    if not changed:
        print(f"'{GLOBAL_CUSTOM_PIPELINE_NAME}' does not route to '{target_pipeline}'. Nothing to revert.")
        sys.exit(0)

    print(f"Removing the processor routing '{GLOBAL_CUSTOM_PIPELINE_NAME}' to '{target_pipeline}'.")

    # Pre-flight simulation -- reverted body is a strict subset of an
    # already-valid pipeline, but simulate anyway for the same reason
    # wire_global_custom.py does: catch surprises before touching the live
    # pipeline, not after.
    print("Running pre-flight simulation on Elasticsearch...")
    validator = IngestPipelineValidator(deployer_config)
    try:
        validator.simulate_pipeline(reverted_body, "synthetic test log for global@custom revert pre-flight simulation")
        print("Pre-flight Simulation Successful.")
    except Exception as e:
        print(f"Pre-flight Simulation Failed: {e}", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(f"[DRY-RUN] Would deploy the following reverted '{GLOBAL_CUSTOM_PIPELINE_NAME}' pipeline:")
        print(json.dumps(reverted_body, indent=2))
        sys.exit(0)

    # Two-pronged deployment, same pattern as wire_global_custom.py
    try:
        es_deployer.deploy(GLOBAL_CUSTOM_PIPELINE_NAME, reverted_body)
        print(f"Step A: Immediate '{GLOBAL_CUSTOM_PIPELINE_NAME}' PUT to Elasticsearch successful.")
    except Exception as e:
        print(f"Step A failed: {e}", file=sys.stderr)
        sys.exit(1)

    temp_file = "/app/data/parsed/global_custom_reverted.json"
    try:
        with open(temp_file, "w", encoding="utf-8") as tf:
            json.dump(reverted_body, tf, indent=4)
        salt_deployer = SaltstackDeployer(deployer_config)
        salt_deployer.deploy_persistently_exact(GLOBAL_CUSTOM_PIPELINE_NAME, temp_file)
        print(f"Step B: Persistent '{GLOBAL_CUSTOM_PIPELINE_NAME}' configuration copied to Saltstack successfully.")
    except Exception as e:
        print(f"Step B failed: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
