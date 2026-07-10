import os
import requests

class ElasticsearchDeployer:
    """Manages queries and updates to the Elasticsearch REST Ingest API."""
    
    def __init__(self, config: dict):
        self.url = f"https://{os.environ['SO_IP']}:{config['elasticsearch']['port']}"
        self.user = os.environ["SO_USER"]
        self.password = os.environ["SO_PASS"]
        self.verify = config["elasticsearch"]["verify_certs"]
        
    def get_deployed_pipeline(self, pipeline_name: str) -> dict:
        """Queries GET /_ingest/pipeline/<name>. Returns None if 404."""
        endpoint = f"{self.url}/_ingest/pipeline/{pipeline_name}"
        response = requests.get(
            endpoint,
            auth=(self.user, self.password),
            verify=self.verify,
            timeout=10
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
        
    def deploy(self, pipeline_name: str, pipeline_json: dict):
        """Sends PUT request to _ingest/pipeline/<name>."""
        endpoint = f"{self.url}/_ingest/pipeline/{pipeline_name}"
        response = requests.put(
            endpoint,
            json=pipeline_json,
            auth=(self.user, self.password),
            verify=self.verify,
            timeout=15
        )
        response.raise_for_status()
