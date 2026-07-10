import json
import requests
import os

class IngestPipelineValidator:
    """Validates pipeline structure using Elasticsearch _simulate API."""
    
    def __init__(self, config: dict):
        self.url = f"https://{os.environ['SO_IP']}:{config['elasticsearch']['port']}"
        self.user = os.environ["SO_USER"]
        self.password = os.environ["SO_PASS"]
        self.verify = config["elasticsearch"]["verify_certs"]
        
    def simulate_pipeline(self, pipeline_json: dict, sample_log: str) -> bool:
        """Sends simulation request to POST /_ingest/pipeline/_simulate."""
        endpoint = f"{self.url}/_ingest/pipeline/_simulate"
        
        simulation_payload = {
            "pipeline": pipeline_json,
            "docs": [
                {
                    "_source": {
                        "message": sample_log
                    }
                }
            ]
        }
        
        response = requests.post(
            endpoint,
            json=simulation_payload,
            auth=(self.user, self.password),
            verify=self.verify,
            timeout=15
        )
        response.raise_for_status()
        
        res_data = response.json()
        
        # Check if the simulation processor failed
        for doc in res_data.get("docs", []):
            if "error" in doc:
                error_details = doc["error"]
                raise ValueError(f"Elasticsearch Pipeline Simulation Failed: {error_details}")
                
        return True
