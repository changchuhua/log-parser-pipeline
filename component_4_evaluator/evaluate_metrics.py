"""Evaluation Orchestrator for calculating log parsing accuracy.

This script loads raw ground truth logs and parsed parser outputs (CSV or JSONL),
aligns them by LineId, corrects formatting anomalies, executes standard and
custom mathematical metric calculators, and logs a summary table.
"""

import os
import glob
import json
import yaml
import pandas as pd
import numpy as np
import logging
import sys

from metrics.oracle_correction import oracle_correct, sort_csv_by_content_order
from metrics.GA_calculator import evaluate as calculate_ga
from metrics.PA_calculator import calculate_parsing_accuracy as calculate_pa
from metrics.ED_calculator import calculate_edit_distance as calculate_ed
from metrics.GD_calculator import calculate_ggd, calculate_pgd
from metrics.PMSS_calculator import calculate_pmss

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("evaluator")

def load_config(config_path='/app/config.yaml'):
    """Loads centralization configuration parameters.

    Args:
        config_path (str): YAML file path. Defaults to '/app/config.yaml'.

    Returns:
        dict: YAML configuration dictionary.
    """
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def load_ground_truth(raw_dir):
    """Searches and compiles all raw CSV ground truth datasets.

    Extracts LineId, Content, and EventTemplate properties.

    Args:
        raw_dir (str): Directory containing ground truth CSV files.

    Returns:
        pd.DataFrame: Aligned ground truth DataFrame.
    """
    csv_files = glob.glob(os.path.join(raw_dir, '*.csv'))
    gt_records = []
    
    for f in csv_files:
        try:
            df = pd.read_csv(f)
            content_col = next((c for c in ['Content', 'message', '_raw'] if c in df.columns), None)
            template_col = next((c for c in ['EventTemplate', 'template'] if c in df.columns), None)
            line_id_col = next((c for c in ['LineId', 'event.id'] if c in df.columns), None)
            
            if content_col and template_col:
                for idx, row in df.iterrows():
                    line_id = str(row[line_id_col]) if line_id_col else str(idx + 1)
                    gt_records.append({
                        'LineId': line_id,
                        'Content': str(row[content_col]).strip(),
                        'EventTemplate': str(row[template_col]).strip()
                    })
        except Exception as e:
            logger.error(f"Error loading raw ground truth from {f}: {e}")
            
    return pd.DataFrame(gt_records)

def main():
    """Main orchestrator that aligns outputs and executes metric calculations."""
    config = load_config()
    raw_dir = 'data/raw'
    parsed_dir = 'data/parsed'
    
    logger.info("Loading ground truth datasets...")
    df_gt = load_ground_truth(raw_dir)
    
    if df_gt.empty:
        logger.warning("Ground truth is empty. GA, PA, ED, GGD, and PGD metrics cannot be calculated.")
        logger.warning("Only PMSS (label-free metric) will be calculated.")
    else:
        logger.info(f"Loaded {len(df_gt)} ground truth log templates.")
        df_gt = oracle_correct(df_gt)
        
    parsed_files = glob.glob(os.path.join(parsed_dir, '*_output.csv')) + glob.glob(os.path.join(parsed_dir, '*.jsonl'))
    
    report = {}
    
    for pf in parsed_files:
        parser_name = os.path.basename(pf).replace('_output.csv', '').replace('.jsonl', '')
        logger.info(f"Evaluating parser: {parser_name}...")
        
        try:
            if pf.endswith('.csv'):
                df_parsed = pd.read_csv(pf)
                df_parsed.rename(columns={'template': 'EventTemplate'}, inplace=True)
            else:
                records = []
                with open(pf, 'r') as f:
                    for line in f:
                        record = json.loads(line.strip())
                        records.append({
                            'LineId': record.get('event', {}).get('id') or record.get('LineId'),
                            'Content': record.get('message') or record.get('Content'),
                            'EventTemplate': record.get('parsed_template') or record.get('EventTemplate')
                        })
                df_parsed = pd.DataFrame(records)
                
            df_parsed['LineId'] = df_parsed['LineId'].astype(str)
            df_parsed['Content'] = df_parsed['Content'].astype(str).str.strip()
            df_parsed['EventTemplate'] = df_parsed['EventTemplate'].astype(str).str.strip()
            
            df_gt_aligned = pd.DataFrame()
            if not df_gt.empty:
                merged = pd.merge(df_gt, df_parsed, on='LineId', suffixes=('_gt', '_parsed'))
                
                df_gt_aligned = merged[['LineId', 'Content_gt', 'EventTemplate_gt']].copy()
                df_gt_aligned.columns = ['LineId', 'Content', 'EventTemplate']
                
                df_parsed_aligned = merged[['LineId', 'Content_parsed', 'EventTemplate_parsed']].copy()
                df_parsed_aligned.columns = ['LineId', 'Content', 'EventTemplate']
            else:
                df_parsed_aligned = df_parsed[['LineId', 'Content', 'EventTemplate']]
                
            metrics = {}
            
            pmss_score = calculate_pmss(df_parsed_aligned)
            metrics['PMSS'] = float(pmss_score)
            
            if not df_gt_aligned.empty:
                ga_score, fga_score = calculate_ga(df_gt_aligned, df_parsed_aligned)
                metrics['GA'] = float(ga_score)
                metrics['FGA'] = float(fga_score)
                
                pa_score = calculate_pa(df_gt_aligned, df_parsed_aligned)
                metrics['PA'] = float(pa_score)
                
                ed_score, ned_score = calculate_ed(df_gt_aligned, df_parsed_aligned)
                metrics['ED'] = float(ed_score)
                metrics['NED'] = float(ned_score)
                
                ggd_score = calculate_ggd(df_gt_aligned, df_parsed_aligned)
                metrics['GGD'] = float(ggd_score)
                
                pgd_score = calculate_pgd(df_gt_aligned, df_parsed_aligned)
                metrics['PGD'] = float(pgd_score)
            else:
                metrics['GA'] = 0.0
                metrics['FGA'] = 0.0
                metrics['PA'] = 0.0
                metrics['ED'] = 0.0
                metrics['NED'] = 0.0
                metrics['GGD'] = 0.0
                metrics['PGD'] = 0.0
                
            # Load profile elapsed time if exists
            time_score = 0.0
            profile_file = os.path.join(parsed_dir, f"{parser_name}_profile.json")
            if os.path.exists(profile_file):
                try:
                    with open(profile_file, 'r', encoding='utf-8') as pf_file:
                        prof_data = json.load(pf_file)
                        time_score = prof_data.get('time_taken_seconds', 0.0)
                except Exception as e:
                    logger.error(f"Error loading profile for {parser_name}: {e}")
            metrics['Time(s)'] = time_score
            
            report[parser_name] = metrics
            
        except Exception as e:
            logger.error(f"Error evaluating {pf}: {e}")
            
    report_file = 'data/evaluation_report.json'
    with open(report_file, 'w') as f:
        json.dump(report, f, indent=4)
        
    logger.info(f"Evaluation report saved to {report_file}")
    
    table_str = "\n" + "="*109 + "\n"
    table_str += f"{'Parser':<20} | {'GA':<10} | {'PA':<10} | {'ED':<10} | {'GGD':<10} | {'PGD':<10} | {'PMSS':<10} | {'Time(s)':<10}\n"
    table_str += "="*109 + "\n"
    for parser, met in report.items():
        table_str += f"{parser:<20} | {met['GA']:<10.4f} | {met['PA']:<10.4f} | {met['ED']:<10.4f} | {met['GGD']:<10.4f} | {met['PGD']:<10.4f} | {met['PMSS']:<10.4f} | {met['Time(s)']:<10.4f}\n"
    table_str += "="*109
    logger.info(table_str)

if __name__ == "__main__":
    main()
