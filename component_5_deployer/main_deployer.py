import os
import json
import sys
import yaml
from dotenv import load_dotenv
from core.compiler import IngestPipelineCompiler
from core.validator import IngestPipelineValidator
from core.es_client import ElasticsearchDeployer
from core.salt_sftp import SaltstackDeployer

def main():
    # Load .env variables
    load_dotenv()
    
    # Check for required environment variables
    required_envs = ["SO_IP", "SO_USER", "SO_PASS", "TAILSCALE_NODE"]
    missing_envs = [env for env in required_envs if not os.environ.get(env)]
    if missing_envs:
        print(f"Error: Missing required environment variables: {', '.join(missing_envs)}. Aborting deployment.", file=sys.stderr)
        sys.exit(1)

    # 1. Load config
    with open("/app/config.yaml", "r") as f:
        config = yaml.safe_load(f)
        
    deployer_config = config["deployer"]
    dry_run = deployer_config["dry_run"]
    pipeline_name = deployer_config["pipeline_name"]
    
    # Overridable so a single hand-picked template (or any alternate JSONL) can
    # be pointed at directly for debugging, without touching the real
    # data/parsed/parsed_loghub_ecs.jsonl output.
    parsed_logs_file = deployer_config.get("parsed_logs_path") or "/app/data/parsed/parsed_loghub_ecs.jsonl"
    temp_pipeline_file = "/app/data/parsed/compiled_pipeline.json"  # Output target
    
    # 2. Compile raw templates to Ingest Pipeline Grok JSON
    print(f"Compiling raw templates from {parsed_logs_file}...")
    compiler = IngestPipelineCompiler()
    try:
        pipeline_json = compiler.build_pipeline_json(parsed_logs_file, pipeline_name)
    except Exception as e:
        print(f"Pipeline Compilation Failed: {e}", file=sys.stderr)
        sys.exit(1)
        
    # 3. Idempotency Check: Fetch currently deployed pipeline
    es_deployer = ElasticsearchDeployer(deployer_config)
    print("Checking if pipeline configuration has changed...")
    try:
        current_deployed = es_deployer.get_deployed_pipeline(pipeline_name)
    except Exception as e:
        print(f"Failed to query deployed pipeline status: {e}", file=sys.stderr)
        current_deployed = None

    # Compare compiled pipeline with deployed pipeline
    is_changed = True
    if current_deployed and pipeline_name in current_deployed:
        existing_pipeline_body = current_deployed[pipeline_name]
        if existing_pipeline_body.get("processors") == pipeline_json.get("processors"):
            is_changed = False
            
    if not is_changed:
        print(f"Pipeline '{pipeline_name}' has not changed. Skipping deployment.")
        sys.exit(0)
        
    # Write temp file for reference
    with open(temp_pipeline_file, "w", encoding="utf-8") as tf:
        json.dump(pipeline_json, tf, indent=4)
    
    # 4. Pre-flight simulation validation (POST /_simulate)
    # Extract first raw log as simulate sample (Must exist, no fallback allowed)
    sample_log = None
    try:
        with open(parsed_logs_file, "r", encoding="utf-8") as f:
            for line in f:
                record = json.loads(line.strip())
                if record.get("message"):
                    sample_log = record["message"]
                    break
    except Exception as e:
        print(f"Failed to read raw logs for simulation: {e}", file=sys.stderr)
        sys.exit(1)
        
    if not sample_log:
        print("Error: No valid raw log message found in parsed logs. Aborting simulation.", file=sys.stderr)
        sys.exit(1)
        
    print(f"Running pre-flight simulation on Elasticsearch with sample log: '{sample_log}'...")
    validator = IngestPipelineValidator(deployer_config)
    try:
        validator.simulate_pipeline(pipeline_json, sample_log)
        print("Pre-flight Simulation Successful.")
    except Exception as e:
        print(f"Pre-flight Simulation Failed: {e}", file=sys.stderr)
        sys.exit(1)
        
    if dry_run:
        print(f"[DRY-RUN] Pipeline '{pipeline_name}' successfully compiled and validated. Skipping deployment.")
        sys.exit(0)
        
    # 5. Two-Pronged Deployment
    # Prong A: Immediate Rest API application
    es_deployer.deploy(pipeline_name, pipeline_json)
    print(f"Step A: Immediate pipeline PUT to Elasticsearch successful.")
    
    # Prong B: Persistent Saltstack transfer
    salt_deployer = SaltstackDeployer(deployer_config)
    salt_deployer.deploy_persistently(pipeline_name, temp_pipeline_file)
    print(f"Step B: Persistent pipeline configuration copied to Saltstack successfully.")

if __name__ == "__main__":
    main()
