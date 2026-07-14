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
    if not os.path.exists(config_path) and config_path == '/app/config.yaml':
        config_path = 'config.yaml'
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
        <div id="summary-cards" class="grid grid-cols-1 md:grid-cols-3 gap-6 mb-8"></div>

        <!-- Charts Section -->
        <div class="grid grid-cols-1 lg:grid-cols-2 gap-8 mb-8">
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

            <!-- Scatter Plot Card -->
            <div class="bg-gray-800 border border-gray-700 rounded-lg p-6">
                <h3 class="text-lg font-semibold mb-4 text-white">Cost vs. Benefit (Scatter)</h3>
                <div class="relative h-96">
                    <canvas id="scatterPlot"></canvas>
                </div>
            </div>

            <!-- Cache Scalability Line Chart Card -->
            <div class="bg-gray-800 border border-gray-700 rounded-lg p-6">
                <h3 class="text-lg font-semibold mb-4 text-white">Cache Scalability (Log Volume vs Invocations)</h3>
                <div class="relative h-96">
                    <canvas id="lineChart"></canvas>
                </div>
            </div>

            <!-- Correlation Heatmap Card -->
            <div class="bg-gray-800 border border-gray-700 rounded-lg p-6 lg:col-span-2">
                <h3 class="text-lg font-semibold mb-4 text-white">Spearman Rank Correlation Matrix</h3>
                <div class="overflow-x-auto">
                    <table class="table-auto w-full text-center border-collapse text-gray-300">
                        <thead>
                            <tr class="border-b border-gray-700">
                                <th class="p-4"></th>
                                <th class="p-4 font-semibold text-white">PMSS</th>
                                <th class="p-4 font-semibold text-white">FGA</th>
                                <th class="p-4 font-semibold text-white">FTA</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr class="border-b border-gray-800">
                                <td class="p-4 font-semibold text-white">PMSS</td>
                                <td class="p-4 bg-indigo-900 text-white font-bold">1.0000</td>
                                <td id="cell-pmss-fga" class="p-4 font-bold">-</td>
                                <td id="cell-pmss-fta" class="p-4 font-bold">-</td>
                            </tr>
                            <tr class="border-b border-gray-800">
                                <td class="p-4 font-semibold text-white">FGA</td>
                                <td id="cell-fga-pmss" class="p-4 font-bold">-</td>
                                <td class="p-4 bg-indigo-900 text-white font-bold">1.0000</td>
                                <td id="cell-fga-fta" class="p-4 font-bold">-</td>
                            </tr>
                            <tr>
                                <td class="p-4 font-semibold text-white">FTA</td>
                                <td id="cell-fta-pmss" class="p-4 font-bold">-</td>
                                <td id="cell-fta-fga" class="p-4 font-bold">-</td>
                                <td class="p-4 bg-indigo-900 text-white font-bold">1.0000</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <script>
        const reportData = {json.dumps(viz_data)};
        const parsers = Object.keys(reportData.summary);
        
        // Build summary cards
        const cardContainer = document.getElementById('summary-cards');
        parsers.forEach(parser => {{
            const summary = reportData.summary[parser];
            const failRate = (summary.failure_rate * 100).toFixed(2) + '%';
            cardContainer.innerHTML += `
                <div class="bg-gray-800 border border-gray-700 rounded-lg p-6">
                    <div class="text-sm font-semibold text-indigo-400 uppercase tracking-wider mb-2">${{parser.toUpperCase()}}</div>
                    <div class="grid grid-cols-2 gap-4">
                        <div>
                            <div class="text-xs text-gray-400">GA / PA</div>
                            <div class="text-lg font-bold text-white">${{(summary.GA * 100).toFixed(1)}}% / ${{(summary.PA * 100).toFixed(1)}}%</div>
                        </div>
                        <div>
                            <div class="text-xs text-gray-400">Time / Tokens</div>
                            <div class="text-lg font-bold text-white">${{Math.round(summary.Time_s)}}s / ${{summary.Tokens.toLocaleString()}}</div>
                        </div>
                        <div>
                            <div class="text-xs text-gray-400">Timeouts / Failures</div>
                            <div class="text-lg font-bold text-white">${{summary.llm_timeouts}} / ${{summary.failed_invocations}}</div>
                        </div>
                        <div>
                            <div class="text-xs text-gray-400">Failure Rate</div>
                            <div class="text-lg font-bold text-red-400">${{failRate}}</div>
                        </div>
                    </div>
                </div>
            `;
        }});

        // 1. Accuracy Profile (Radar)
        const radarColors = [
            {{ bg: 'rgba(99, 110, 250, 0.2)', border: 'rgba(99, 110, 250, 1)' }},
            {{ bg: 'rgba(239, 85, 59, 0.2)', border: 'rgba(239, 85, 59, 1)' }},
            {{ bg: 'rgba(0, 204, 150, 0.2)', border: 'rgba(0, 204, 150, 1)' }}
        ];
        
        const radarDatasets = parsers.map((parser, idx) => {{
            const summary = reportData.summary[parser];
            const color = radarColors[idx % radarColors.length];
            return {{
                label: parser.toUpperCase(),
                data: [summary.GA, summary.PA, summary.FGA, summary.FTA, summary.PMSS],
                backgroundColor: color.bg,
                borderColor: color.border,
                borderWidth: 2
            }};
        }});

        new Chart(document.getElementById('radarChart'), {{
            type: 'radar',
            data: {{
                labels: ['GA', 'PA', 'FGA', 'FTA', 'PMSS'],
                datasets: radarDatasets
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

        // 2. Bar Chart Datasets (Sensitivity Corrections)
        const barColors = [
            {{ pa: '#636EFA', fta: '#EF553B' }},
            {{ pa: '#33b5e5', fta: '#ff4444' }},
            {{ pa: '#00C851', fta: '#ffbb33' }}
        ];
        const barDatasets = [];
        parsers.forEach((parser, idx) => {{
            const sens = reportData.visualizations.grouped_bar_chart_sensitivity[parser];
            if (!sens) return;
            const levels = Object.keys(sens);
            const paData = levels.map(l => sens[l].PA);
            const ftaData = levels.map(l => sens[l].FTA);
            const color = barColors[idx % barColors.length];
            
            barDatasets.push({{
                label: `${{parser.toUpperCase()}} PA`,
                data: paData,
                backgroundColor: color.pa,
                borderRadius: 4
            }});
            barDatasets.push({{
                label: `${{parser.toUpperCase()}} FTA`,
                data: ftaData,
                backgroundColor: color.fta,
                borderRadius: 4
            }});
        }});

        const tempSens = reportData.visualizations.grouped_bar_chart_sensitivity[parsers[0]];
        const sensLevels = tempSens ? Object.keys(tempSens) : [];

        new Chart(document.getElementById('barChart'), {{
            type: 'bar',
            data: {{
                labels: sensLevels.map(l => l.toUpperCase()),
                datasets: barDatasets
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

        // 3. Scatter Plot (Cost vs Benefit)
        const scatterColors = ['#636EFA', '#EF553B', '#00CC96'];
        const scatterDatasets = parsers.map((parser, idx) => {{
            const summary = reportData.summary[parser];
            return {{
                label: parser.toUpperCase(),
                data: [{{ x: summary.Time_s, y: summary.FGA }}],
                backgroundColor: scatterColors[idx % scatterColors.length],
                pointRadius: 10,
                pointHoverRadius: 12
            }};
        }});

        new Chart(document.getElementById('scatterPlot'), {{
            type: 'scatter',
            data: {{
                datasets: scatterDatasets
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    x: {{
                        type: 'linear',
                        position: 'bottom',
                        title: {{ display: true, text: 'Execution Time (seconds)', color: '#9CA3AF' }},
                        ticks: {{ color: '#9CA3AF' }},
                        grid: {{ color: 'rgba(255, 255, 255, 0.1)' }}
                    }},
                    y: {{
                        title: {{ display: true, text: 'FGA (Few-shot Group Accuracy)', color: '#9CA3AF' }},
                        ticks: {{ color: '#9CA3AF' }},
                        grid: {{ color: 'rgba(255, 255, 255, 0.1)' }},
                        suggestedMin: 0,
                        suggestedMax: 1
                    }}
                }},
                plugins: {{
                    legend: {{ labels: {{ color: '#FFF' }} }}
                }}
            }}
        }});

        // 4. Line Chart (Cache Scalability)
        const lineColors = ['#636EFA', '#EF553B', '#00CC96'];
        const lineDatasets = parsers.map((parser, idx) => {{
            const hData = reportData.visualizations.line_graph_cache_scalability[parser]?.history || [];
            return {{
                label: parser.toUpperCase(),
                data: hData.map(h => ({{ x: h.log_volume, y: h.llm_invocations }})),
                borderColor: lineColors[idx % lineColors.length],
                backgroundColor: 'transparent',
                borderWidth: 2.5,
                tension: 0.1,
                pointRadius: 4
            }};
        }});

        new Chart(document.getElementById('lineChart'), {{
            type: 'line',
            data: {{
                datasets: lineDatasets
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    x: {{
                        type: 'linear',
                        position: 'bottom',
                        title: {{ display: true, text: 'Log Ingestion Volume', color: '#9CA3AF' }},
                        ticks: {{ color: '#9CA3AF' }},
                        grid: {{ color: 'rgba(255, 255, 255, 0.1)' }}
                    }},
                    y: {{
                        title: {{ display: true, text: 'Cumulative LLM Invocations', color: '#9CA3AF' }},
                        ticks: {{ color: '#9CA3AF' }},
                        grid: {{ color: 'rgba(255, 255, 255, 0.1)' }}
                    }}
                }},
                plugins: {{
                    legend: {{ labels: {{ color: '#FFF' }} }}
                }}
            }}
        }});

        // 5. Correlation Heatmap Grid Population
        const matrix = reportData.correlation_matrix || {{ pmss_fga: 0, pmss_fta: 0, fga_fta: 0 }};
        const cellColor = (val) => {{
            const absVal = Math.abs(val);
            return val >= 0 ? `rgba(99, 110, 250, ${{absVal}})` : `rgba(239, 85, 59, ${{absVal}})`;
        }};
        const setCell = (id, val) => {{
            const cell = document.getElementById(id);
            if (cell) {{
                cell.innerText = val.toFixed(4);
                cell.style.backgroundColor = cellColor(val);
                cell.style.color = '#FFF';
            }}
        }};
        
        setCell('cell-pmss-fga', matrix.pmss_fga);
        setCell('cell-fga-pmss', matrix.pmss_fga);
        
        setCell('cell-pmss-fta', matrix.pmss_fta);
        setCell('cell-fta-pmss', matrix.pmss_fta);
        
        setCell('cell-fga-fta', matrix.fga_fta);
        setCell('cell-fta-fga', matrix.fga_fta);
    </script>
</body>
</html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html_content)

def load_profile_metrics(parsed_dir, parser_name):
    prof_metrics = {
        'Time(s)': 0.0,
        'LLM Invocations': 0,
        'Total Tokens': 0,
        'history': [],
        'model_used': 'unknown-model',
        'method_used': 'unknown-method',
        'llm_timeouts': 0,
        'failed_invocations': 0,
        'failure_rate': 0.0,
        'cache_hits': 0,
        'log_volume': 0,
        'cache_hit_rate': 0.0,
        'throughput_pps': 0.0
    }
    profile_file = os.path.join(parsed_dir, f"{parser_name}_profile.json")
    if os.path.exists(profile_file):
        try:
            with open(profile_file, 'r', encoding='utf-8') as pf_file:
                prof_data = json.load(pf_file)
                prof_metrics['Time(s)'] = prof_data.get('time_taken_seconds', 0.0)
                prof_metrics['LLM Invocations'] = prof_data.get('llm_invocations', 0)
                prof_metrics['Total Tokens'] = prof_data.get('total_tokens', 0)
                prof_metrics['history'] = prof_data.get('history', [])
                prof_metrics['model_used'] = prof_data.get('model_used', 'unknown-model')
                prof_metrics['method_used'] = prof_data.get('method_used', 'unknown-method')
                prof_metrics['llm_timeouts'] = prof_data.get('llm_timeouts', 0)
                prof_metrics['failed_invocations'] = prof_data.get('failed_invocations', 0)
                
                c_hits = prof_data.get('cache_hits', 0)
                l_vol = prof_data.get('log_volume', 0)
                
                # Backwards compatibility fallback from history
                if c_hits == 0 or l_vol == 0:
                    history = prof_metrics['history']
                    if history:
                        c_hits = history[-1].get('cache_hits', 0)
                        l_vol = history[-1].get('log_volume', 0)
                
                prof_metrics['cache_hits'] = c_hits
                prof_metrics['log_volume'] = l_vol
                prof_metrics['cache_hit_rate'] = (c_hits / l_vol) if l_vol > 0 else 0.0
                
                t_taken = prof_metrics['Time(s)']
                prof_metrics['throughput_pps'] = (l_vol / t_taken) if t_taken > 0.0 else 0.0
                
                total_attempts = prof_metrics['LLM Invocations'] + prof_metrics['llm_timeouts'] + prof_metrics['failed_invocations']
                prof_metrics['failure_rate'] = (prof_metrics['llm_timeouts'] + prof_metrics['failed_invocations']) / total_attempts if total_attempts > 0 else 0.0
        except Exception as e:
            logger.error(f"Error loading profile for {parser_name}: {e}")
    return prof_metrics


def compute_metrics_for_pair(df_gt_aligned_raw, df_parsed_aligned_raw):
    metrics = {}
    
    # 1. Spaced alignment (default comparison)
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
        if not df_parsed_aligned_raw.empty:
            gt_lvl, parsed_lvl = apply_sensitivity_correction(df_gt_aligned_raw, df_parsed_aligned_raw, lvl)
            pa_lvl = calculate_pa(gt_lvl, parsed_lvl)
            fta_lvl = calculate_fta(gt_lvl, parsed_lvl)
        else:
            pa_lvl = 0.0
            fta_lvl = 0.0
        sensitivity[lvl] = {
            'PA': float(pa_lvl),
            'FTA': float(fta_lvl)
        }
        logger.info(f"Correction Level '{lvl}' - PA: {pa_lvl:.4f} | FTA: {fta_lvl:.4f}")
    metrics['sensitivity'] = sensitivity
    return metrics


def main():
    """Main orchestrator that aligns outputs and executes metric calculations."""
    config = load_config()
    directories = config.get('directories', {})
    dataset_name = directories.get('dataset_name', 'loghub')
    
    raw_base = directories.get('input_dir', 'data/raw')
    raw_dir = os.path.join(raw_base, dataset_name)
    
    parsed_dir = os.path.join('data/parsed', dataset_name)
    
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
            
            df_gt_aligned_raw = pd.DataFrame(columns=['LineId', 'Content', 'EventTemplate'])
            if not df_gt_raw.empty:
                merged = pd.merge(df_gt_raw, df_parsed, on='LineId', suffixes=('_gt', '_parsed'))
                
                df_gt_aligned_raw = merged[['LineId', 'Content_gt', 'EventTemplate_gt']].copy()
                df_gt_aligned_raw.columns = ['LineId', 'Content', 'EventTemplate']
                
                df_parsed_aligned_raw = merged[['LineId', 'Content_parsed', 'EventTemplate_parsed']].copy()
                df_parsed_aligned_raw.columns = ['LineId', 'Content', 'EventTemplate']
            else:
                df_parsed_aligned_raw = df_parsed[['LineId', 'Content', 'EventTemplate']]
                
            prof_metrics = load_profile_metrics(parsed_dir, parser_name)
            
            if dataset_name == 'loghub' and not df_parsed_aligned_raw.empty:
                # Segment sub-datasets based on LineId prefixes
                # LineId is prefix_id (e.g. "Apache_123")
                sub_datasets = sorted(list(df_parsed_aligned_raw['LineId'].apply(lambda x: str(x).split('_')[0]).unique()))
                
                if len(sub_datasets) > 1 or (len(sub_datasets) == 1 and sub_datasets[0] != parser_name):
                    logger.info(f"LogHub dataset detected. Evaluating {len(sub_datasets)} sub-datasets individually...")
                    
                    # 1. Overall evaluation
                    logger.info("Computing Overall evaluation metrics...")
                    overall_metrics = compute_metrics_for_pair(df_gt_aligned_raw, df_parsed_aligned_raw)
                    overall_metrics.update(prof_metrics)
                    report[f"{parser_name}_Overall"] = overall_metrics
                    
                    # 2. Per sub-dataset evaluation
                    for sub_ds in sub_datasets:
                        logger.info(f"Evaluating sub-dataset: {sub_ds}...")
                        df_gt_sub = df_gt_aligned_raw[df_gt_aligned_raw['LineId'].str.startswith(sub_ds + '_')]
                        df_parsed_sub = df_parsed_aligned_raw[df_parsed_aligned_raw['LineId'].str.startswith(sub_ds + '_')]
                        
                        if df_gt_sub.empty or df_parsed_sub.empty:
                            logger.warning(f"Sub-dataset {sub_ds} is empty. Skipping.")
                            continue
                            
                        sub_metrics = compute_metrics_for_pair(df_gt_sub, df_parsed_sub)
                        sub_metrics.update(prof_metrics)
                        report[f"{parser_name}_{sub_ds}"] = sub_metrics
                    continue
            
            # Default single dataset evaluation
            metrics = compute_metrics_for_pair(df_gt_aligned_raw, df_parsed_aligned_raw)
            metrics.update(prof_metrics)
            report[parser_name] = metrics
            
        except Exception as e:
            logger.error(f"Error evaluating {pf}: {e}")
            
    # 4. Calculate Spearman rank correlations
    from scipy.stats import spearmanr
    import math
    
    pmss_vals = []
    fga_vals = []
    fta_vals = []
    
    for parser, met in report.items():
        pmss_vals.append(met.get('PMSS', 0.0))
        fga_vals.append(met.get('FGA', 0.0))
        fta_vals.append(met.get('FTA', 0.0))
        
    corr_pmss_fga, _ = spearmanr(pmss_vals, fga_vals) if len(pmss_vals) > 1 else (0.0, 1.0)
    corr_pmss_fta, _ = spearmanr(pmss_vals, fta_vals) if len(pmss_vals) > 1 else (0.0, 1.0)
    corr_fga_fta, _ = spearmanr(fga_vals, fta_vals) if len(fga_vals) > 1 else (0.0, 1.0)
    
    if math.isnan(corr_pmss_fga): corr_pmss_fga = 0.0
    if math.isnan(corr_pmss_fta): corr_pmss_fta = 0.0
    if math.isnan(corr_fga_fta): corr_fga_fta = 0.0
    
    logger.info("Spearman Rank Correlation Coefficients across evaluated parsers:")
    logger.info(f"  PMSS vs. FGA: {corr_pmss_fga:.4f}")
    logger.info(f"  PMSS vs. FTA: {corr_pmss_fta:.4f}")
    logger.info(f"  FGA vs. FTA: {corr_fga_fta:.4f}")

    report_file = 'data/evaluation_report.json'
    with open(report_file, 'w') as f:
        # Standard reports
        std_report = {k: {mk: mv for mk, mv in v.items() if mk not in ['sensitivity', 'history', 'model_used', 'method_used']} for k, v in report.items()}
        json.dump(std_report, f, indent=4)
        
    logger.info(f"Evaluation report saved to {report_file}")
    
    # Generate schema-compliant visualization report
    viz_report = {
        "summary": {},
        "correlation_matrix": {
            "pmss_fga": float(corr_pmss_fga),
            "pmss_fta": float(corr_pmss_fta),
            "fga_fta": float(corr_fga_fta)
        },
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
            "Tokens": met['Total Tokens'],
            "cache_hit_rate": met.get('cache_hit_rate', 0.0),
            "throughput_pps": met.get('throughput_pps', 0.0),
            "llm_timeouts": met.get('llm_timeouts', 0),
            "failed_invocations": met.get('failed_invocations', 0),
            "failure_rate": met.get('failure_rate', 0.0)
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
        archive_dir = os.path.join('data/archive', dataset_name, model_used, method_used)
        try:
            os.makedirs(archive_dir, exist_ok=True)
            shutil.copy(report_file, os.path.join(archive_dir, f"evaluation_report_{run_timestamp}.json"))
            shutil.copy(viz_report_file, os.path.join(archive_dir, f"evaluation_report_viz_{run_timestamp}.json"))
            if os.path.exists(html_report_file):
                shutil.copy(html_report_file, os.path.join(archive_dir, f"report_{run_timestamp}.html"))
            logger.info(f"Archived report files to {archive_dir} as evaluation_report[...]_{run_timestamp}.json/.html")
        except Exception as e:
            logger.error(f"Error archiving report files for {parser}: {e}")
    
    table_str = "\n" + "="*209 + "\n"
    table_str += f"{'Parser':<25} | {'GA':<8} | {'PA':<8} | {'FGA':<8} | {'FTA':<8} | {'ED':<8} | {'PMSS':<8} | {'Time(s)':<8} | {'LLM Calls':<10} | {'Tokens':<8} | {'Cache Hit %':<12} | {'Throughput':<12} | {'Timeouts':<8} | {'Failures':<8} | {'Fail Rate':<9}\n"
    table_str += "="*209 + "\n"
    for parser, met in report.items():
        cache_hit_pct = met.get('cache_hit_rate', 0.0) * 100
        throughput = met.get('throughput_pps', 0.0)
        table_str += f"{parser:<25} | {met['GA']:<8.4f} | {met['PA']:<8.4f} | {met['FGA']:<8.4f} | {met['FTA']:<8.4f} | {met['ED']:<8.4f} | {met['PMSS']:<8.4f} | {met['Time(s)']:<8.2f} | {met['LLM Invocations']:<10} | {met['Total Tokens']:<8} | {cache_hit_pct:<11.2f}% | {throughput:<10.2f} l/s | {met.get('llm_timeouts', 0):<8} | {met.get('failed_invocations', 0):<8} | {met.get('failure_rate', 0.0)*100:<8.2f}%\n"
    table_str += "="*209
    logger.info(table_str)

if __name__ == "__main__":
    main()
