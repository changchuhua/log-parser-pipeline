"""Security Onion log extractor module.

This module sequentially extracts Dead Letter Queue (DLQ) logs via SSH over Tailscale
and unmapped event logs directly from Elasticsearch using scrolling queries.
"""

import os
import json
import subprocess
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

DLQ_BASE_PATH = "/nsm/logstash/dead_letter_queue"
DLQ_DECODER_BIN = "/usr/local/bin/logstash-dlq-decode"


def _unwrap_cbor_tagged(value):
    """Recursively strips the Java/JRuby class-name tags that
    logstash-dlq-decode's CBOR decoding leaves in place — e.g.
    ["org.logstash.ConvertedMap", {...}] — down to plain Python types.
    """
    if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and '.' in value[0]:
        tag, payload = value
        if tag in ('org.logstash.ConvertedMap', 'java.util.HashMap'):
            return {k: _unwrap_cbor_tagged(v) for k, v in payload.items()}
        if tag == 'org.logstash.ConvertedList':
            return [_unwrap_cbor_tagged(v) for v in payload]
        if tag == 'org.jruby.RubyNil':
            return None
        return payload  # RubyString / Timestamp / etc. — already a plain scalar
    if isinstance(value, dict):
        return {k: _unwrap_cbor_tagged(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_unwrap_cbor_tagged(v) for v in value]
    return value


def _decode_dlq_segment(segment_bytes):
    """Runs one DLQ segment file's raw bytes through the local
    logstash-dlq-decode binary and yields (message, reason) for each
    successfully decoded record.

    Must be called once per segment file, never on concatenated multi-file
    bytes — each segment has its own leading version byte and 32KB-block
    framing that would misalign across a concatenated stream. Raises on any
    decode failure; the caller treats that as a skippable per-file warning
    rather than aborting the whole extraction, since the segment Logstash is
    actively appending to will predictably fail here (truncated mid-record).
    """
    proc = subprocess.run(
        [DLQ_DECODER_BIN],
        input=segment_bytes,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode('utf-8', errors='replace').strip())
    for line in proc.stdout.decode('utf-8', errors='replace').splitlines():
        if not line.strip():
            continue
        decoded = json.loads(line)
        real_event = _unwrap_cbor_tagged(decoded.get('event'))
        data = real_event.get('DATA', {}) if isinstance(real_event, dict) else {}
        message = data.get('message')
        if message is None:
            message = json.dumps(data)
        yield message, decoded.get('reason')


def _fetch_file_bytes(ssh, remote_path, sudo_prefix):
    """Reads one remote file's raw bytes over the same SSH exec channel used
    elsewhere in this module (rather than SFTP), so `use_sudo` — which only
    applies to a shell command, not the separate SFTP subsystem — still
    works for hosts relying on a sudoers grant instead of group access."""
    command = f"{sudo_prefix}cat {remote_path}"
    stdin, stdout, stderr = ssh.exec_command(command)
    channel = stdout.channel
    chunks = []
    while True:
        chunk = channel.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
    err = stderr.read().decode('utf-8', errors='replace').strip()
    return b"".join(chunks), err


def extract_dlq_logs(tailscale_user, tailscale_host, output_file, tailscale_pass=None, use_sudo=False):
    """Establishes an SSH connection, decodes, and streams Security Onion
    Logstash DLQ logs.

    Lists every Logstash pipeline's DLQ directory via a wildcard, since
    Security Onion writes dead-letter entries to whichever pipeline rejected
    the event, and the actual pipeline name(s) vary across installations
    (e.g. "main" vs "manager") rather than following a fixed convention —
    hardcoding specific names would silently miss data on installations that
    don't match the guess. Only closed segments (`*.log`) are read; the
    segment Logstash is actively writing has a `*.log.tmp` suffix and can't
    be decoded mid-write.

    Args:
        tailscale_user (str): SSH login user name.
        tailscale_host (str): Tailscale target node host or IP.
        output_file (str): Local path to write DLQ logs.
        tailscale_pass (str, optional): SSH login password. Leave unset (None
            or empty) to fall back to SSH-agent/key-based auth instead —
            required when the target uses Tailscale SSH rather than a
            password-authenticating sshd, since Tailscale SSH has no password
            to supply.
        use_sudo (bool): Prefix remote `ls`/`cat` commands with `sudo`.
            Defaults to False — group-based read access (adding the SSH user
            to the `logstash` group) is the recommended setup and needs no
            sudo. Set True only for hosts still relying on a sudoers
            NOPASSWD grant.

    Returns:
        int: Number of logs successfully extracted.
    """
    logger.info(f"Extracting DLQ logs via SSH from {tailscale_user}@{tailscale_host}...")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    row_count = 0
    try:
        if tailscale_pass:
            ssh.connect(hostname=tailscale_host, username=tailscale_user, password=tailscale_pass)
        else:
            ssh.connect(hostname=tailscale_host, username=tailscale_user)

        sudo_prefix = "sudo " if use_sudo else ""

        list_command = f"{sudo_prefix}sh -c 'ls {DLQ_BASE_PATH}/*/*.log 2>/dev/null'"
        stdin, stdout, stderr = ssh.exec_command(list_command)
        segment_paths = [
            line.strip() for line in stdout.read().decode('utf-8', errors='replace').splitlines()
            if line.strip()
        ]
        logger.info(f"Found {len(segment_paths)} closed DLQ segment file(s) to decode.")

        with open(output_file, 'w', encoding='utf-8') as f:
            for segment_path in segment_paths:
                raw_bytes, err = _fetch_file_bytes(ssh, segment_path, sudo_prefix)
                if err:
                    logger.warning(f"SSH stderr fetching {segment_path}: {err}")
                if not raw_bytes:
                    continue
                try:
                    for message, reason in _decode_dlq_segment(raw_bytes):
                        record = {
                            "message": message,
                            "event": {
                                "id": f"so_dlq_{row_count}",
                                "dataset": "so_dlq",
                                "reason": reason,
                            }
                        }
                        f.write(json.dumps(record) + "\n")
                        row_count += 1
                except Exception as e:
                    logger.warning(
                        f"Skipping {segment_path}: decode failed ({e}). This is expected "
                        f"if the file was rotated mid-decode or is otherwise truncated."
                    )
                    continue

        logger.info(f"Extracted {row_count} DLQ logs to {output_file}.")
        return row_count

    except Exception as e:
        logger.error(f"Error extracting DLQ logs: {e}")
        return 0
    finally:
        ssh.close()

def load_es_extract_state(state_file):
    """Reads the last-seen @timestamp cursor from a previous extract_unmapped_logs()
    run, if any. Returns None if no state file exists yet or it's unreadable —
    the caller falls back to the configured relative lookback_time in that case."""
    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            return json.load(f).get('last_timestamp')
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def save_es_extract_state(state_file, last_timestamp):
    """Persists the last-seen @timestamp cursor so the next extract_unmapped_logs()
    run starts from there instead of re-querying the full lookback window."""
    with open(state_file, 'w', encoding='utf-8') as f:
        json.dump({'last_timestamp': last_timestamp}, f)


def extract_unmapped_logs(es_ip, es_user, es_pass, batch_size, since, output_file):
    """Extracts unmapped logs from Elasticsearch using a scroll-loop search.

    Queries Elasticsearch for documents having a message field but no event
    category, excluding performance/system metrics.

    Args:
        es_ip (str): IP address of the Elasticsearch instance.
        es_user (str): Elasticsearch API basic auth user.
        es_pass (str): Elasticsearch API basic auth password.
        batch_size (int): Max size per search chunk.
        since (str): Lower @timestamp bound (exclusive) — either a resolved
            cursor from a previous run's state file, or config.yaml's
            relative lookback_time (e.g. 'now-24h') on a first run with no
            prior state.
        output_file (str): Path to write search hits. Opened in append mode:
            combined with the `since` cursor advancing on each successful
            run, repeated runs accumulate new records only rather than
            re-pulling and re-appending the same overlapping window.

    Returns:
        tuple[int, str | None]: Number of records written, and the maximum
        @timestamp seen this run (None if none were seen) — the caller
        persists that as the cursor for the next run.
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
                # "gt" (exclusive), not "gte": `since` is either a prior run's
                # own max-seen timestamp (already extracted — re-including it
                # would duplicate that one record) or the initial relative
                # lookback_time, where inclusive-vs-exclusive doesn't matter
                # in practice. Note: if multiple records share the exact same
                # @timestamp as the cursor, any not included in the batch
                # that set the cursor would be silently skipped on the next
                # run — an accepted tradeoff for a simple single-field cursor.
                "filter": [{"range": {"@timestamp": {"gt": since, "lte": "now"}}}]
            }
        },
        "_source": ["@timestamp", "agent.name", "event.dataset", "message", "event.original"]
    }

    row_count = 0
    max_timestamp = None
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
                    source = hit.get('_source', {})
                    # Inject a dataset-prefixed event.id so main_parser.py can
                    # attribute/group these records the same way it does for
                    # Component 1's ECS-standardized output.
                    event_obj = source.get('event')
                    if not isinstance(event_obj, dict):
                        event_obj = {}
                    event_obj['id'] = f"so_unmapped_{row_count}"
                    source['event'] = event_obj
                    f.write(json.dumps(source) + '\n')
                    row_count += 1
                    ts = source.get('@timestamp')
                    if ts and (max_timestamp is None or ts > max_timestamp):
                        max_timestamp = ts

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
        return row_count, max_timestamp

    except Exception as e:
        logger.error(f"Error querying Elasticsearch: {e}")
        return row_count, max_timestamp

def main():
    """Main executor that loads environment variables and runs DLQ/unmapped log extraction."""
    load_dotenv()
    config = load_config()
    
    directories = config.get('directories', {})
    extractor_cfg = config.get('extractor', {})
    # extractor.dataset_name lets Component 2 write to a different dataset
    # folder than directories.dataset_name (used by Components 1/3/4/5), so
    # switching where Component 2 pulls to doesn't require flipping the
    # global dataset scope for every other component. Falls back to
    # directories.dataset_name when unset.
    dataset_name = extractor_cfg.get('dataset_name') or directories.get('dataset_name', 'loghub')
    # Scoped by dataset_name, matching Component 1's output layout, so
    # main_parser.py's `data/processed/{dataset_name}/*.jsonl` glob picks
    # these up automatically instead of needing a manual move.
    output_dir = os.path.join(directories.get('output_dir', 'data/processed'), dataset_name)
    os.makedirs(output_dir, exist_ok=True)

    batch_size = extractor_cfg.get('batch_size', 5000)
    lookback_time = extractor_cfg.get('lookback_time', 'now-24h')
    use_sudo = extractor_cfg.get('dlq_use_sudo', False)

    so_ip = os.environ.get('SO_IP')
    so_user = os.environ.get('SO_USER')
    so_pass = os.environ.get('SO_PASS')
    ts_node = os.environ.get('TAILSCALE_NODE')
    ts_user = os.environ.get('TS_USER', 'admin')
    ts_pass = os.environ.get('TS_PASS')  # optional: leave unset for Tailscale SSH / key-agent auth

    dlq_out_file = os.path.join(output_dir, 'so_dlq_logs.jsonl')
    unmapped_out_file = os.path.join(output_dir, 'unmapped_fallback_logs.jsonl')
    es_state_file = os.path.join(output_dir, 'es_extract_state.json')

    dlq_count = 0
    if ts_node:
        dlq_count = extract_dlq_logs(ts_user, ts_node, dlq_out_file, tailscale_pass=ts_pass, use_sudo=use_sudo)
    else:
        logger.warning("TAILSCALE_NODE not set in .env. Skipping DLQ extraction.")

    es_count = 0
    if so_ip and so_user and so_pass:
        # Resume from the previous run's cursor if one exists, so repeated
        # runs accumulate only new records instead of re-pulling and
        # re-appending the same overlapping lookback_time window.
        since = load_es_extract_state(es_state_file) or lookback_time
        es_count, max_timestamp = extract_unmapped_logs(so_ip, so_user, so_pass, batch_size, since, unmapped_out_file)
        if max_timestamp:
            save_es_extract_state(es_state_file, max_timestamp)
    else:
        logger.warning("SO_IP, SO_USER, or SO_PASS not set in .env. Skipping ES extraction.")
    
    logger.info("Summary - DLQ logs extracted: %d, Unmapped ES logs extracted: %d", dlq_count, es_count)

if __name__ == "__main__":
    main()
