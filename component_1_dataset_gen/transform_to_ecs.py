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
from pathlib import Path

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("dataset_generator")

def process_loghub(input_file, output_file):
    """Processes raw LogHub-2.0 CSV logs and converts them to ECS JSONL.

    Maps columns Date+Time to @timestamp, Content to message, Level to log.level,
    Component to log.logger, and LineId to event.id.

    Args:
        input_file (str): Path to the raw LogHub CSV file.
        output_file (str): Path to write the standardized ECS JSONL file.
    """
    df = pd.read_csv(input_file)
    
    with open(output_file, 'w') as f:
        for _, row in df.iterrows():
            date = str(row.get('Date', ''))
            time = str(row.get('Time', ''))
            # Combine Date and Time
            timestamp = f"{date} {time}".strip()
            
            ecs_log = {
                "@timestamp": timestamp,
                "message": str(row.get('Content', '')),
                "log": {
                    "level": str(row.get('Level', '')),
                    "logger": str(row.get('Component', ''))
                },
                "event": {
                    "id": str(row.get('LineId', ''))
                }
            }
            f.write(json.dumps(ecs_log) + '\n')
    logger.info(f"Processed LogHub data and saved to {output_file}")


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
