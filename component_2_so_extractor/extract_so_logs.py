import os
import json
import paramiko
import requests
import urllib3
import yaml
from requests.auth import HTTPBasicAuth
from dotenv import load_dotenv

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def load_config(config_path='/app/config.yaml'):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def extract_dlq_logs(tailscale_user, tailscale_host, output_file):
    print(f"[*] Extracting DLQ logs via SSH from {tailscale_user}@{tailscale_host}...")
    
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
            print(f"[!] SSH Stderr: {err}")
            
        print(f"[*] Extracted {row_count} DLQ logs to {output_file}.")
        return row_count
        
    except Exception as e:
        print(f"[!] Error extracting DLQ logs: {e}")
        return 0
    finally:
        ssh.close()

def extract_unmapped_logs(es_ip, es_user, es_pass, batch_size, lookback_time, output_file):
    print(f"[*] Extracting unmapped logs from Elasticsearch at {es_ip}...")
    
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
            
        print(f"[*] Extracted {row_count} unmapped logs to {output_file}.")
        return row_count

    except Exception as e:
        print(f"[!] Error querying Elasticsearch: {e}")
        return row_count

def main():
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
        print("[!] TAILSCALE_NODE not set in .env. Skipping DLQ extraction.")
        
    es_count = 0
    if so_ip and so_user and so_pass:
        es_count = extract_unmapped_logs(so_ip, so_user, so_pass, batch_size, lookback_time, unmapped_out_file)
    else:
        print("[!] SO_IP, SO_USER, or SO_PASS not set in .env. Skipping ES extraction.")
    
    print("\n--- Summary ---")
    print(f"Total DLQ logs extracted: {dlq_count}")
    print(f"Total Unmapped ES logs extracted: {es_count}")

if __name__ == "__main__":
    main()
