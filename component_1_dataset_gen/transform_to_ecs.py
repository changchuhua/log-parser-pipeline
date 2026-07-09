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


def main():
    """Main execution function that parses CLI args and runs standardization routines."""
    parser = argparse.ArgumentParser(description="Transform datasets to ECS JSONL format")
    parser.add_argument('--loghub', type=str, help='Path to LogHub CSV file')
    parser.add_argument('--botsv3', type=str, help='Path to BOTSv3 CSV file')
    parser.add_argument('--out-dir', type=str, default='data/', help='Output directory for ECS JSONL files')
    
    args = parser.parse_args()
    
    os.makedirs(args.out_dir, exist_ok=True)
    
    processed = False
    
    if args.loghub and os.path.exists(args.loghub):
        out_file = os.path.join(args.out_dir, 'loghub_ecs.jsonl')
        process_loghub(args.loghub, out_file)
        processed = True
        
    if args.botsv3 and os.path.exists(args.botsv3):
        out_file = os.path.join(args.out_dir, 'botsv3_ecs.jsonl')
        process_botsv3(args.botsv3, out_file)
        processed = True
        
    if not processed:
        logger.error("No valid input files provided. Please pass --loghub or --botsv3 with valid paths.")
        logger.error("Example: python transform_to_ecs.py --loghub data/loghub.csv --botsv3 data/botsv3.csv")

if __name__ == "__main__":
    main()
