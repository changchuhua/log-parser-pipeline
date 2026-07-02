import os
import json
import argparse
import paramiko
import requests
import urllib3
from requests.auth import HTTPBasicAuth

# Suppress insecure request warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def extract_dlq_logs(tailscale_user, tailscale_host, output_file, ssh_key_path=None):
    """
    Task A: DLQ Log Extraction via Tailscale SSH
    """
    print(f"[*] Extracting DLQ logs via SSH from {tailscale_user}@{tailscale_host}...")
    
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    row_count = 0
    try:
        connect_kwargs = {
            "hostname": tailscale_host,
            "username": tailscale_user
        }
        if ssh_key_path and os.path.exists(ssh_key_path):
            connect_kwargs["key_filename"] = ssh_key_path
            
        ssh.connect(**connect_kwargs)
        
        # Extract Logstash DLQ contents. Assuming they are text based JSON/lines for this task.
        command = "sudo cat /nsm/logstash/dead_letter_queue/main/*"
        stdin, stdout, stderr = ssh.exec_command(command)
        
        with open(output_file, 'w', encoding='utf-8') as f:
            for line in stdout:
                f.write(line)
                row_count += 1
                
        err = stderr.read().decode().strip()
        if err:
            print(f"[!] SSH Stderr: {err}")
            
        print(f"[*] Task A complete. Extracted {row_count} lines to {output_file}.")
        return row_count
        
    except Exception as e:
        print(f"[!] Error extracting DLQ logs: {e}")
        return 0
    finally:
        ssh.close()

def extract_unmapped_logs(es_ip, es_user, es_pass, output_file):
    """
    Task B: Unparseable Logs via Elasticsearch API (Scroll)
    Translates the Bash script logic to native Python.
    """
    print(f"[*] Extracting unmapped logs from Elasticsearch at {es_ip}...")
    
    base_url = f"https://{es_ip}:9200"
    auth = HTTPBasicAuth(es_user, es_pass)
    headers = {"Content-Type": "application/json"}
    
    batch_size = 5000
    lookback_time = "now-24h"
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
            
        print(f"[*] Task B complete. Extracted {row_count} unmapped logs to {output_file}.")
        return row_count

    except Exception as e:
        print(f"[!] Error querying Elasticsearch: {e}")
        return row_count

def main():
    parser = argparse.ArgumentParser(description="Security Onion Log Extractor")
    parser.add_argument('--ts-user', default=os.environ.get('TS_USER', 'admin'), help='Tailscale SSH User')
    parser.add_argument('--ts-host', default=os.environ.get('TS_HOST', 'so-manager'), help='Tailscale Hostname/IP')
    parser.add_argument('--es-ip', default=os.environ.get('ES_IP', '192.168.1.100'), help='Elasticsearch IP')
    parser.add_argument('--es-user', default=os.environ.get('ES_USER', 'admin@domain.com'), help='Elasticsearch User')
    parser.add_argument('--es-pass', default=os.environ.get('ES_PASS', 'YourPasswordHere'), help='Elasticsearch Password')
    parser.add_argument('--out-dir', default='data/', help='Output directory for extracted logs')
    
    args = parser.parse_args()
    
    os.makedirs(args.out_dir, exist_ok=True)
    
    dlq_out_file = os.path.join(args.out_dir, 'so_dlq_logs.jsonl')
    unmapped_out_file = os.path.join(args.out_dir, 'unmapped_fallback_logs.jsonl')
    
    dlq_count = extract_dlq_logs(args.ts_user, args.ts_host, dlq_out_file)
    es_count = extract_unmapped_logs(args.es_ip, args.es_user, args.es_pass, unmapped_out_file)
    
    print("\n--- Summary ---")
    print(f"Total DLQ logs extracted: {dlq_count}")
    print(f"Total Unmapped ES logs extracted: {es_count}")

if __name__ == "__main__":
    main()
