"""Dataset generator module for standardizing LogHub and BOTSv3 logs to ECS format.

This module maps heterogeneous raw log formats to the standardized Elastic Common
Schema (ECS) and exports them in JSON Lines (JSONL) format.
"""

import pandas as pd
import json
import os
import argparse
import logging
import sys
import glob
import random
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("dataset_generator")

def process_loghub(input_path, output_file):
    """Processes raw LogHub-2.0 CSV logs and converts them to ECS JSONL.

    Accepts either a single CSV file or a directory containing CSV files.
    Standardizes them, shuffles them per-dataset, prepends the dataset name to LineId,
    and interleaves them in a round-robin sequence.

    Args:
        input_path (str): Path to a raw LogHub CSV file or a directory containing them.
        output_file (str): Path to write the standardized ECS JSONL file.
    """
    csv_files = []
    if os.path.isdir(input_path):
        csv_files = sorted(glob.glob(os.path.join(input_path, '*.csv')))
    else:
        csv_files = [input_path]

    sources_data = []
    for f in csv_files:
        try:
            # Limit rows read per file to prevent memory exhaustion (OOM) on massive files
            df = pd.read_csv(f, nrows=100000)
            dataset_name = os.path.basename(f).replace('_sample.csv', '').replace('_full.log_structured.csv', '').replace('.csv', '')
            
            records = []
            for idx, row in df.iterrows():
                date = str(row.get('Date', ''))
                time_val = str(row.get('Time', ''))
                timestamp = f"{date} {time_val}".strip()
                
                raw_line_id = str(row.get('LineId', '')) if 'LineId' in row else str(idx + 1)
                prefixed_id = f"{dataset_name}_{raw_line_id}"
                
                ecs_log = {
                    "@timestamp": timestamp,
                    "message": str(row.get('Content', '')),
                    "log": {
                        "level": str(row.get('Level', '')),
                        "logger": str(row.get('Component', ''))
                    },
                    "event": {
                        "id": prefixed_id
                    }
                }
                records.append(ecs_log)
                
            # Randomize logs for this specific dataset
            random.shuffle(records)
            sources_data.append(records)
            logger.info(f"Loaded {len(records)} logs from {os.path.basename(f)} (prefixed with {dataset_name})")
        except Exception as e:
            logger.error(f"Error loading LogHub raw file {f}: {e}")

    # Interleave round-robin
    interleaved = []
    if sources_data:
        max_len = max(len(s) for s in sources_data)
        for i in range(max_len):
            for s in sources_data:
                if i < len(s):
                    interleaved.append(s[i])

    with open(output_file, 'w', encoding='utf-8') as out_f:
        for record in interleaved:
            out_f.write(json.dumps(record) + '\n')
            
    logger.info(f"Processed {len(csv_files)} LogHub files, randomized and interleaved {len(interleaved)} total logs saved to {output_file}")


def process_botsv3(input_file, output_file):
    """Processes raw Splunk BOTSv3 CSV logs and converts them to ECS JSONL.

    Maps columns _time to @timestamp, _raw to message, sourcetype to event.dataset,
    and host to host.name.

    Args:
        input_file (str): Path to the raw Splunk BOTSv3 CSV file.
        output_file (str): Path to write the standardized ECS JSONL file.
    """
    df = pd.read_csv(input_file)
    
    with open(output_file, 'w') as f:
        for _, row in df.iterrows():
            ecs_log = {
                "@timestamp": str(row.get('_time', '')),
                "message": str(row.get('_raw', '')),
                "event": {
                    "dataset": str(row.get('sourcetype', ''))
                },
                "host": {
                    "name": str(row.get('host', ''))
                }
            }
            f.write(json.dumps(ecs_log) + '\n')
    logger.info(f"Processed BOTSv3 data and saved to {output_file}")


import yaml

def load_config(config_path='config.yaml'):
    if not os.path.exists(config_path) and os.path.exists('/app/config.yaml'):
        config_path = '/app/config.yaml'
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    return {}

def main():
    """Main execution function that parses CLI args and runs standardization routines."""
    config = load_config()
    directories = config.get('directories', {})
    dataset_name = directories.get('dataset_name', 'loghub')
    
    # Resolve default directories
    raw_base = directories.get('input_dir', 'data/raw')
    processed_base = directories.get('output_dir', 'data/processed')
    
    default_input_dir = os.path.join(raw_base, dataset_name)
    default_out_dir = os.path.join(processed_base, dataset_name)

    parser = argparse.ArgumentParser(description="Transform datasets to ECS JSONL format")
    parser.add_argument('--loghub', type=str, help='Path to LogHub CSV file')
    parser.add_argument('--botsv3', type=str, help='Path to BOTSv3 CSV file')
    parser.add_argument('--out-dir', type=str, default=None, help='Output directory for ECS JSONL files')
    
    args = parser.parse_args()
    
    out_dir = args.out_dir or default_out_dir
    os.makedirs(out_dir, exist_ok=True)
    
    processed = False
    
    # If no CLI paths provided, fallback to dataset_name default directory
    loghub_path = args.loghub
    botsv3_path = args.botsv3
    
    if not loghub_path and not botsv3_path:
        if dataset_name == 'loghub':
            loghub_path = default_input_dir
        elif dataset_name == 'botsv3':
            # Check if there is a botsv3 csv in the default directory
            csvs = glob.glob(os.path.join(default_input_dir, '*.csv'))
            if csvs:
                botsv3_path = csvs[0]
            else:
                botsv3_path = os.path.join(default_input_dir, 'botsv3.csv')
    
    if loghub_path and os.path.exists(loghub_path):
        out_file = os.path.join(out_dir, 'loghub_ecs.jsonl')
        process_loghub(loghub_path, out_file)
        processed = True
        
    if botsv3_path and os.path.exists(botsv3_path):
        out_file = os.path.join(out_dir, 'botsv3_ecs.jsonl')
        process_botsv3(botsv3_path, out_file)
        processed = True
        
    if not processed:
        logger.error(f"No valid input files found in {default_input_dir} or provided via CLI.")

if __name__ == "__main__":
    main()
