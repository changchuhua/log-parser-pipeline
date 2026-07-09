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
from metrics.FTA_calculator import calculate_fta

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

def load_ground_truth(raw_dir, nrows=None):
    """Searches and compiles all raw CSV ground truth datasets.

    Extracts LineId, Content, and EventTemplate properties, prefixing LineId
    with the source dataset name to guarantee global uniqueness.

    Args:
        raw_dir (str): Directory containing ground truth CSV files.
        nrows (int, optional): Number of rows to read per file.

    Returns:
        pd.DataFrame: Aligned ground truth DataFrame.
    """
    csv_files = glob.glob(os.path.join(raw_dir, '*.csv'))
    gt_dfs = []
    
    for f in csv_files:
        try:
            df = pd.read_csv(f, nrows=nrows)
            content_col = next((c for c in ['Content', 'message', '_raw'] if c in df.columns), None)
            template_col = next((c for c in ['EventTemplate', 'template'] if c in df.columns), None)
            line_id_col = next((c for c in ['LineId', 'event.id'] if c in df.columns), None)
            
            dataset_name = os.path.basename(f).replace('_sample.csv', '').replace('_full.log_structured.csv', '').replace('.csv', '')
            
            if content_col and template_col:
                if line_id_col:
                    raw_line_ids = df[line_id_col].astype(str)
                else:
                    raw_line_ids = (df.index + 1).astype(str)
                
                prefixed_ids = dataset_name + "_" + raw_line_ids
                
                temp_df = pd.DataFrame({
                    'LineId': prefixed_ids,
                    'Content': df[content_col].astype(str).str.strip(),
                    'EventTemplate': df[template_col].astype(str).str.strip()
                })
                gt_dfs.append(temp_df)
        except Exception as e:
            logger.error(f"Error loading raw ground truth from {f}: {e}")
            
    if gt_dfs:
        return pd.concat(gt_dfs, ignore_index=True)
    return pd.DataFrame(columns=['LineId', 'Content', 'EventTemplate'])


def apply_sensitivity_correction(df_gt, df_parsed, level):
    """Applies target correction level to aligned dataframes.

    Args:
        df_gt (pd.DataFrame): Ground truth DataFrame.
        df_parsed (pd.DataFrame): Parsed results DataFrame.
        level (str): Type of alignment check ('raw', 'spaced', 'lowercase', 'regex_clean').

    Returns:
        tuple: (df_gt_c, df_parsed_c) modified DataFrames.
    """
    df_gt_c = df_gt.copy()
    df_parsed_c = df_parsed.copy()
    
    if level == 'raw':
        # Pure string comparison without change
        pass
    elif level == 'spaced':
        # Default space collapse normalization
        df_gt_c['EventTemplate'] = df_gt_c['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()
        df_parsed_c['EventTemplate'] = df_parsed_c['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()
    elif level == 'lowercase':
        # Spaced + case insensitivity
        df_gt_c['EventTemplate'] = df_gt_c['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip().str.lower()
        df_parsed_c['EventTemplate'] = df_parsed_c['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip().str.lower()
    elif level == 'regex_clean':
        # Spaced + lowercase + trailing punctuation & enclosing symbol removal
        clean_pattern = r'[.\s,:;()\[\]{}|]+$'
        df_gt_c['EventTemplate'] = df_gt_c['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip().str.replace(clean_pattern, '', regex=True).str.lower()
        df_parsed_c['EventTemplate'] = df_parsed_c['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip().str.replace(clean_pattern, '', regex=True).str.lower()
        
    return df_gt_c, df_parsed_c
def generate_html_report(viz_data, output_path='data/report.html'):
    """Generates a self-contained interactive HTML dashboard from visualization metrics.

    Args:
        viz_data (dict): The visualization metrics report dictionary.
        output_path (str): The destination file path.
    """
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pipeline Evaluation Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
</head>
<body class="bg-gray-900 text-gray-100 min-h-screen p-8">
    <div class="max-w-7xl mx-auto">
        <header class="mb-8 border-b border-gray-800 pb-4">
            <h1 class="text-3xl font-bold tracking-tight text-white">Log Parser Evaluation Dashboard</h1>
            <p class="text-gray-400 mt-1">Interactive parsing accuracy and efficiency comparison reports.</p>
        </header>

        <!-- Dynamic Summary Cards -->
        <div id="summary-cards" class="grid grid-cols-1 md:grid-cols-4 gap-6 mb-8"></div>

        <!-- Charts Section -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8">
            <!-- Radar Chart Card -->
            <div class="bg-gray-800 border border-gray-700 rounded-lg p-6">
                <h3 class="text-lg font-semibold mb-4 text-white">Accuracy Profile (Radar)</h3>
                <div class="relative h-96">
                    <canvas id="radarChart"></canvas>
                </div>
            </div>

            <!-- Bar Chart Card -->
            <div class="bg-gray-800 border border-gray-700 rounded-lg p-6">
                <h3 class="text-lg font-semibold mb-4 text-white">Sensitivity Corrections Comparison</h3>
                <div class="relative h-96">
                    <canvas id="barChart"></canvas>
                </div>
            </div>
        </div>
    </div>

    <script>
        const reportData = {json.dumps(viz_data)};
        const parser = Object.keys(reportData.summary)[0];
        const summary = reportData.summary[parser];
        
        // Build cards
        const cardContainer = document.getElementById('summary-cards');
        const metrics = [
            {{ name: 'Group Accuracy (GA)', val: (summary.GA * 100).toFixed(1) + '%' }},
            {{ name: 'Parsing Accuracy (PA)', val: (summary.PA * 100).toFixed(1) + '%' }},
            {{ name: 'Few-shot Template (FTA)', val: (summary.FTA * 100).toFixed(1) + '%' }},
            {{ name: 'LLM Invocations', val: summary.LLM_Calls.toLocaleString() }}
        ];
        metrics.forEach(m => {{
            cardContainer.innerHTML += `
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-6">
                    <div class="text-sm font-medium text-gray-400 truncate">${{m.name}}</div>
                    <div class="mt-2 text-3xl font-semibold text-white">${{m.val}}</div>
                </div>
            `;
        }});

        // Render Radar
        new Chart(document.getElementById('radarChart'), {{
            type: 'radar',
            data: {{
                labels: ['GA', 'PA', 'FGA', 'FTA', 'PMSS'],
                datasets: [{{
                    label: parser.toUpperCase(),
                    data: [summary.GA, summary.PA, summary.FGA, summary.FTA, summary.PMSS],
                    backgroundColor: 'rgba(99, 110, 250, 0.2)',
                    borderColor: 'rgba(99, 110, 250, 1)',
                    borderWidth: 2
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    r: {{
                        angleLines: {{ color: 'rgba(255, 255, 255, 0.1)' }},
                        grid: {{ color: 'rgba(255, 255, 255, 0.1)' }},
                        pointLabels: {{ color: '#9CA3AF', font: {{ size: 12 }} }},
                        ticks: {{ backdropColor: 'transparent', color: '#9CA3AF' }},
                        suggestedMin: 0,
                        suggestedMax: 1
                    }}
                }},
                plugins: {{
                    legend: {{ labels: {{ color: '#FFF' }} }}
                }}
            }}
        }});

        // Render Bar Chart
        const sens = reportData.visualizations.grouped_bar_chart_sensitivity[parser];
        const levels = Object.keys(sens);
        const paData = levels.map(l => sens[l].PA);
        const ftaData = levels.map(l => sens[l].FTA);

        new Chart(document.getElementById('barChart'), {{
            type: 'bar',
            data: {{
                labels: levels.map(l => l.toUpperCase()),
                datasets: [
                    {{
                        label: 'Parsing Accuracy (PA)',
                        data: paData,
                        backgroundColor: '#636EFA',
                        borderRadius: 4
                    }},
                    {{
                        label: 'Few-shot Template (FTA)',
                        data: ftaData,
                        backgroundColor: '#EF553B',
                        borderRadius: 4
                    }}
                ]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    x: {{ grid: {{ display: false }}, ticks: {{ color: '#9CA3AF' }} }},
                    y: {{ grid: {{ color: 'rgba(255, 255, 255, 0.1)' }}, ticks: {{ color: '#9CA3AF' }}, suggestedMin: 0, suggestedMax: 1 }}
                }},
                plugins: {{
                    legend: {{ labels: {{ color: '#FFF' }} }}
                }}
            }}
        }});
    </script>
</body>
</html>"""
    
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

def main():
    """Main orchestrator that aligns outputs and executes metric calculations."""
    config = load_config()
    raw_dir = 'data/raw'
    parsed_dir = 'data/parsed'
    
    # Retrieve nrows limit from centralized config
    nrows = config.get('evaluator', {}).get('nrows', None)
    
    logger.info("Loading ground truth datasets...")
    df_gt_raw = load_ground_truth(raw_dir, nrows=nrows)
    
    if df_gt_raw.empty:
        logger.warning("Ground truth is empty. Accurate metrics cannot be calculated.")
    else:
        logger.info(f"Loaded {len(df_gt_raw)} ground truth log templates.")
        
    parsed_files = glob.glob(os.path.join(parsed_dir, '*_output.csv')) + glob.glob(os.path.join(parsed_dir, '*.jsonl'))
    parsed_files = [f for f in parsed_files if os.path.basename(f) not in ('llm_debug.jsonl', 'unified_parser.log')]
    
    report = {}
    
    for pf in parsed_files:
        parser_name = os.path.basename(pf).replace('_output.csv', '').replace('.jsonl', '')
        logger.info(f"Evaluating parser: {parser_name}...")
        
        try:
            if pf.endswith('.csv'):
                df_parsed = pd.read_csv(pf, nrows=nrows)
                df_parsed.rename(columns={'template': 'EventTemplate'}, inplace=True)
            else:
                records = []
                with open(pf, 'r') as f:
                    for idx, line in enumerate(f):
                        if nrows is not None and idx >= nrows:
                            break
                        record = json.loads(line.strip())
                        records.append({
                            'LineId': record.get('event', {}).get('id') or record.get('LineId'),
                            'Content': record.get('message') or record.get('Content'),
                            'EventTemplate': record.get('parsed_template') or record.get('EventTemplate')
                        })
                df_parsed = pd.DataFrame(records)
                
            df_parsed['LineId'] = df_parsed['LineId'].astype(str)
            df_parsed['Content'] = df_parsed['Content'].astype(str).str.strip()
            df_parsed['EventTemplate'] = (
                df_parsed['EventTemplate']
                .astype(str)
                .str.strip()
                .str.replace(r'<(?!\*+>)[^>]+>', '<*>', regex=True)
            )
            
            df_gt_aligned_raw = pd.DataFrame()
            if not df_gt_raw.empty:
                merged = pd.merge(df_gt_raw, df_parsed, on='LineId', suffixes=('_gt', '_parsed'))
                
                df_gt_aligned_raw = merged[['LineId', 'Content_gt', 'EventTemplate_gt']].copy()
                df_gt_aligned_raw.columns = ['LineId', 'Content', 'EventTemplate']
                
                df_parsed_aligned_raw = merged[['LineId', 'Content_parsed', 'EventTemplate_parsed']].copy()
                df_parsed_aligned_raw.columns = ['LineId', 'Content', 'EventTemplate']
            else:
                df_parsed_aligned_raw = df_parsed[['LineId', 'Content', 'EventTemplate']]
                
            metrics = {}
            
            # 1. Spaced alignment (default default comparison)
            logger.info("Applying standard 'spaced' alignment/normalization...")
            df_gt_std, df_parsed_std = apply_sensitivity_correction(df_gt_aligned_raw, df_parsed_aligned_raw, 'spaced')
            
            logger.info("Computing PMSS (Precomputed Metric Silhouette Score)...")
            pmss_score = calculate_pmss(df_parsed_std)
            metrics['PMSS'] = float(pmss_score)
            logger.info(f"PMSS score: {pmss_score:.4f}")
            
            if not df_gt_std.empty:
                logger.info("Computing Group Accuracy (GA & FGA)...")
                ga_score, fga_score = calculate_ga(df_gt_std, df_parsed_std)
                metrics['GA'] = float(ga_score)
                metrics['FGA'] = float(fga_score)
                logger.info(f"GA: {ga_score:.4f} | FGA: {fga_score:.4f}")
                
                logger.info("Computing Parsing Accuracy (PA)...")
                pa_score = calculate_pa(df_gt_std, df_parsed_std)
                metrics['PA'] = float(pa_score)
                logger.info(f"PA: {pa_score:.4f}")
                
                logger.info("Computing FTA (Few-shot Template Accuracy)...")
                fta_score = calculate_fta(df_gt_std, df_parsed_std)
                metrics['FTA'] = float(fta_score)
                logger.info(f"FTA: {fta_score:.4f}")
                
                logger.info("Computing Edit Distance (ED & NED)...")
                ed_score, ned_score = calculate_ed(df_gt_std, df_parsed_std)
                metrics['ED'] = float(ed_score)
                metrics['NED'] = float(ned_score)
                logger.info(f"ED: {ed_score:.4f} | NED: {ned_score:.4f}")
                
                logger.info("Computing GGD & PGD...")
                ggd_score = calculate_ggd(df_gt_std, df_parsed_std)
                metrics['GGD'] = float(ggd_score)
                
                pgd_score = calculate_pgd(df_gt_std, df_parsed_std)
                metrics['PGD'] = float(pgd_score)
                logger.info(f"GGD: {ggd_score:.4f} | PGD: {pgd_score:.4f}")
            else:
                metrics['GA'] = 0.0
                metrics['FGA'] = 0.0
                metrics['PA'] = 0.0
                metrics['FTA'] = 0.0
                metrics['ED'] = 0.0
                metrics['NED'] = 0.0
                metrics['GGD'] = 0.0
                metrics['PGD'] = 0.0
                
            # 2. Sensitivity corrections
            logger.info("Applying and evaluating sensitivity corrections...")
            sensitivity = {}
            for lvl in ['raw', 'spaced', 'lowercase', 'regex_clean']:
                logger.info(f"Evaluating sensitivity correction level: '{lvl}'...")
                gt_lvl, parsed_lvl = apply_sensitivity_correction(df_gt_aligned_raw, df_parsed_aligned_raw, lvl)
                pa_lvl = calculate_pa(gt_lvl, parsed_lvl)
                fta_lvl = calculate_fta(gt_lvl, parsed_lvl)
                sensitivity[lvl] = {
                    'PA': float(pa_lvl),
                    'FTA': float(fta_lvl)
                }
                logger.info(f"Correction Level '{lvl}' - PA: {pa_lvl:.4f} | FTA: {fta_lvl:.4f}")
            metrics['sensitivity'] = sensitivity
            
            # 3. Load profile and cumulative history
            time_score = 0.0
            llm_invocations = 0
            total_tokens = 0
            history = []
            model_used = "unknown-model"
            method_used = "unknown-method"
            profile_file = os.path.join(parsed_dir, f"{parser_name}_profile.json")
            if os.path.exists(profile_file):
                try:
                    with open(profile_file, 'r', encoding='utf-8') as pf_file:
                        prof_data = json.load(pf_file)
                        time_score = prof_data.get('time_taken_seconds', 0.0)
                        llm_invocations = prof_data.get('llm_invocations', 0)
                        total_tokens = prof_data.get('total_tokens', 0)
                        history = prof_data.get('history', [])
                        model_used = prof_data.get('model_used', model_used)
                        method_used = prof_data.get('method_used', method_used)
                except Exception as e:
                    logger.error(f"Error loading profile for {parser_name}: {e}")
            metrics['Time(s)'] = time_score
            metrics['LLM Invocations'] = llm_invocations
            metrics['Total Tokens'] = total_tokens
            metrics['history'] = history
            metrics['model_used'] = model_used
            metrics['method_used'] = method_used
            
            report[parser_name] = metrics
            
        except Exception as e:
            logger.error(f"Error evaluating {pf}: {e}")
            
    report_file = 'data/evaluation_report.json'
    with open(report_file, 'w') as f:
        # Standard reports
        std_report = {k: {mk: mv for mk, mv in v.items() if mk not in ['sensitivity', 'history', 'model_used', 'method_used']} for k, v in report.items()}
        json.dump(std_report, f, indent=4)
        
    logger.info(f"Evaluation report saved to {report_file}")
    
    # Generate schema-compliant visualization report
    viz_report = {
        "summary": {},
        "visualizations": {
            "radar_chart": {},
            "scatter_plot": {},
            "grouped_bar_chart_sensitivity": {},
            "correlation_heatmap": {},
            "line_graph_cache_scalability": {}
        }
    }
    
    for parser, met in report.items():
        viz_report["summary"][parser] = {
            "FGA": met['FGA'],
            "PA": met['PA'],
            "PMSS": met['PMSS'],
            "FTA": met['FTA'],
            "GA": met['GA'],
            "Time_s": met['Time(s)'],
            "LLM_Calls": met['LLM Invocations'],
            "Tokens": met['Total Tokens']
        }
        viz_report["visualizations"]["radar_chart"][parser] = {
            "FGA": met['FGA'],
            "PA": met['PA'],
            "PMSS": met['PMSS'],
            "FTA": met['FTA']
        }
        viz_report["visualizations"]["scatter_plot"][parser] = {
            "effectiveness": {
                "PMSS": met['PMSS'],
                "FGA": met['FGA']
            },
            "efficiency": {
                "Time_s": met['Time(s)'],
                "Tokens": met['Total Tokens']
            }
        }
        viz_report["visualizations"]["grouped_bar_chart_sensitivity"][parser] = met['sensitivity']
        viz_report["visualizations"]["correlation_heatmap"][parser] = {
            "PMSS": met['PMSS'],
            "FGA": met['FGA'],
            "FTA": met['FTA']
        }
        viz_report["visualizations"]["line_graph_cache_scalability"][parser] = {
            "history": met['history']
        }
        
    viz_report_file = 'data/evaluation_report_viz.json'
    with open(viz_report_file, 'w') as f:
        json.dump(viz_report, f, indent=4)
    logger.info(f"Visualization metrics report saved to {viz_report_file}")

    # Generate interactive HTML dashboard report
    html_report_file = 'data/report.html'
    try:
        generate_html_report(viz_report, html_report_file)
        logger.info(f"Interactive HTML dashboard saved to {html_report_file}")
    except Exception as e:
        logger.error(f"Error generating HTML dashboard: {e}")

    # Copy files to archive with execution timestamp
    import datetime
    import shutil
    run_timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for parser, met in report.items():
        model_used = met.get('model_used', 'unknown-model')
        method_used = met.get('method_used', 'unknown-method')
        archive_dir = os.path.join('data/archive', model_used, method_used)
        try:
            os.makedirs(archive_dir, exist_ok=True)
            shutil.copy(report_file, os.path.join(archive_dir, f"evaluation_report_{run_timestamp}.json"))
            shutil.copy(viz_report_file, os.path.join(archive_dir, f"evaluation_report_viz_{run_timestamp}.json"))
            if os.path.exists(html_report_file):
                shutil.copy(html_report_file, os.path.join(archive_dir, f"report_{run_timestamp}.html"))
            logger.info(f"Archived report files to {archive_dir} as evaluation_report[...]_{run_timestamp}.json/.html")
        except Exception as e:
            logger.error(f"Error archiving report files for {parser}: {e}")
    
    table_str = "\n" + "="*149 + "\n"
    table_str += f"{'Parser':<20} | {'GA':<10} | {'PA':<10} | {'FGA':<10} | {'FTA':<10} | {'ED':<10} | {'PMSS':<10} | {'Time(s)':<10} | {'LLM Calls':<10} | {'Tokens':<10}\n"
    table_str += "="*149 + "\n"
    for parser, met in report.items():
        table_str += f"{parser:<20} | {met['GA']:<10.4f} | {met['PA']:<10.4f} | {met['FGA']:<10.4f} | {met['FTA']:<10.4f} | {met['ED']:<10.4f} | {met['PMSS']:<10.4f} | {met['Time(s)']:<10.4f} | {met['LLM Invocations']:<10} | {met['Total Tokens']:<10}\n"
    table_str += "="*149
    logger.info(table_str)

if __name__ == "__main__":
    main()
