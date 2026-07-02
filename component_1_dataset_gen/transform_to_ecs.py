import pandas as pd
import json
import os
import argparse
from pathlib import Path

def process_loghub(input_file, output_file):
    """
    For LogHub-2.0: map Date+Time to @timestamp, Content to message, 
    Level to log.level, Component to log.logger, and LineId to event.id.
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
    print(f"[*] Processed LogHub data and saved to {output_file}")


def process_botsv3(input_file, output_file):
    """
    For Splunk BOTSv3: map _time to @timestamp, _raw to message, 
    sourcetype to event.dataset, host to host.name.
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
    print(f"[*] Processed BOTSv3 data and saved to {output_file}")


def main():
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
        print("No valid input files provided. Please pass --loghub or --botsv3 with valid paths.")
        print("Example: python transform_to_ecs.py --loghub data/loghub.csv --botsv3 data/botsv3.csv")

if __name__ == "__main__":
    main()
