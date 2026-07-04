"""Security Onion log extractor module.

This module sequentially extracts Dead Letter Queue (DLQ) logs via SSH over Tailscale
and unmapped event logs directly from Elasticsearch using scrolling queries.
"""

import os
import json
import paramiko
import requests
import urllib3
import yaml
import logging
import sys
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("so_extractor")

def load_config(config_path='/app/config.yaml'):
    """Loads centralized pipeline behavioral configuration from a YAML file.

    Args:
        config_path (str): File path to the YAML configuration. Defaults to '/app/config.yaml'.

    Returns:
        dict: Parsed configuration dictionary.
    """
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def extract_dlq_logs(tailscale_user, tailscale_host, output_file):
    """Establishes an SSH connection and streams Security Onion Logstash DLQ logs.

    Args:
        tailscale_user (str): SSH login user name.
        tailscale_host (str): Tailscale target node host or IP.
        output_file (str): Local path to write DLQ logs.

    Returns:
        int: Number of logs successfully extracted.
    """
    logger.info(f"Extracting DLQ logs via SSH from {tailscale_user}@{tailscale_host}...")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    row_count = 0
    try:
        ssh.connect(hostname=tailscale_host, username=tailscale_user)
        command = "sudo cat /nsm/logstash/dead_letter_queue/main/*"
        stdin, stdout, stderr = ssh.exec_command(command)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            for line in stdout:
                f.write(line)
                row_count += 1
                
        err = stderr.read().decode().strip()
        if err:
            logger.warning(f"SSH Stderr: {err}")
            
        logger.info(f"Extracted {row_count} DLQ logs to {output_file}.")
        return row_count
        
    except Exception as e:
        logger.error(f"Error extracting DLQ logs: {e}")
        return 0
    finally:
        ssh.close()

def extract_unmapped_logs(es_ip, es_user, es_pass, batch_size, lookback_time, output_file):
    """Extracts unmapped logs from Elasticsearch using a scroll-loop search.

    Queries Elasticsearch for documents having a message field but no event
    category, excluding performance/system metrics.

    Args:
        es_ip (str): IP address of the Elasticsearch instance.
        es_user (str): Elasticsearch API basic auth user.
        es_pass (str): Elasticsearch API basic auth password.
        batch_size (int): Max size per search chunk.
        lookback_time (str): Time range filter (e.g. 'now-24h').
        output_file (str): Path to write search hits.

    Returns:
        int: Total number of records successfully written.
    """
    logger.info(f"Extracting unmapped logs from Elasticsearch at {es_ip}...")
    
    base_url = f"https://{es_ip}:9200"
    auth = HTTPBasicAuth(es_user, es_pass)
    headers = {"Content-Type": "application/json"}
    
    lucene_query = "_exists_:message AND NOT _exists_:event.category AND NOT event.dataset:(elastic_agent* OR windows.perfmon* OR system.cpu*)"
    
    search_payload = {
        "size": batch_size,
        "query": {
            "bool": {
                "must": [{"query_string": {"query": lucene_query}}],
                "filter": [{"range": {"@timestamp": {"gte": lookback_time, "lte": "now"}}}]
            }
        },
        "_source": ["@timestamp", "agent.name", "event.dataset", "message", "event.original"]
    }
    
    row_count = 0
    try:
        response = requests.get(
            f"{base_url}/_search?scroll=2m",
            auth=auth,
            verify=False,
            headers=headers,
            json=search_payload
        )
        response.raise_for_status()
        data = response.json()
        
        scroll_id = data.get('_scroll_id')
        hits = data.get('hits', {}).get('hits', [])
        
        with open(output_file, 'a', encoding='utf-8') as f:
            while hits:
                for hit in hits:
                    f.write(json.dumps(hit.get('_source', {})) + '\n')
                    row_count += 1
                
                scroll_payload = {
                    "scroll": "2m",
                    "scroll_id": scroll_id
                }
                scroll_response = requests.get(
                    f"{base_url}/_search/scroll",
                    auth=auth,
                    verify=False,
                    headers=headers,
                    json=scroll_payload
                )
                scroll_response.raise_for_status()
                scroll_data = scroll_response.json()
                scroll_id = scroll_data.get('_scroll_id')
                hits = scroll_data.get('hits', {}).get('hits', [])
        
        if scroll_id:
            requests.delete(
                f"{base_url}/_search/scroll",
                auth=auth,
                verify=False,
                headers=headers,
                json={"scroll_id": scroll_id}
            )
            
        logger.info(f"Extracted {row_count} unmapped logs to {output_file}.")
        return row_count
        
    except Exception as e:
        logger.error(f"Error querying Elasticsearch: {e}")
        return row_count

def main():
    """Main executor that loads environment variables and runs DLQ/unmapped log extraction."""
    load_dotenv()
    config = load_config()
    
    output_dir = config.get('directories', {}).get('output_dir', 'data/processed')
    os.makedirs(output_dir, exist_ok=True)
    
    batch_size = config.get('extractor', {}).get('batch_size', 5000)
    lookback_time = config.get('extractor', {}).get('lookback_time', 'now-24h')
    
    so_ip = os.environ.get('SO_IP')
    so_user = os.environ.get('SO_USER')
    so_pass = os.environ.get('SO_PASS')
    ts_node = os.environ.get('TAILSCALE_NODE')
    ts_user = os.environ.get('TS_USER', 'admin')
    
    dlq_out_file = os.path.join(output_dir, 'so_dlq_logs.jsonl')
    unmapped_out_file = os.path.join(output_dir, 'unmapped_fallback_logs.jsonl')
    
    dlq_count = 0
    if ts_node:
        dlq_count = extract_dlq_logs(ts_user, ts_node, dlq_out_file)
    else:
        logger.warning("TAILSCALE_NODE not set in .env. Skipping DLQ extraction.")
        
    es_count = 0
    if so_ip and so_user and so_pass:
        es_count = extract_unmapped_logs(so_ip, so_user, so_pass, batch_size, lookback_time, unmapped_out_file)
    else:
        logger.warning("SO_IP, SO_USER, or SO_PASS not set in .env. Skipping ES extraction.")
    
    logger.info("Summary - DLQ logs extracted: %d, Unmapped ES logs extracted: %d", dlq_count, es_count)

if __name__ == "__main__":
    main()
