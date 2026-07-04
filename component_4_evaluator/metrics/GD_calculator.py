"""Granularity Distance metric calculators.

Provides Group Granularity Distance (GGD) and Parsing Granularity Distance (PGD)
calculators to evaluate alignment discrepancies between parsed templates and oracle templates.
"""

import pandas as pd
import numpy as np

def calculate_ggd(df_gt, df_parsed):
    """Calculates the Group Granularity Distance (GGD).

    GGD measures the ratio discrepancy between the number of generated templates
    and ground truth oracle templates.

    Args:
        df_gt (pd.DataFrame): Ground truth DataFrame.
        df_parsed (pd.DataFrame): Parsed results DataFrame.

    Returns:
        float: Computed GGD score.
    """
    merged = pd.merge(df_gt, df_parsed, on='LineId', suffixes=('_gt', '_parsed'))
    if merged.empty:
        return 0.0
        
    N_generated = merged['EventTemplate_parsed'].nunique()
    N_oracle = merged['EventTemplate_gt'].nunique()
    
    if N_oracle == 0:
        return 0.0
        
    return abs(N_generated - N_oracle) / N_oracle

def calculate_pgd(df_gt, df_parsed):
    """Calculates the Parsing Granularity Distance (PGD).

    PGD maps each generated template to its most frequent matching ground truth
    template and averages the token-length differences.

    Args:
        df_gt (pd.DataFrame): Ground truth DataFrame.
        df_parsed (pd.DataFrame): Parsed results DataFrame.

    Returns:
        float: Average token-length differences (PGD).
    """
    merged = pd.merge(df_gt, df_parsed, on='LineId', suffixes=('_gt', '_parsed'))
    if merged.empty:
        return 0.0
        
    mapping = {}
    for parsed_template, group in merged.groupby('EventTemplate_parsed'):
        most_frequent_gt = group['EventTemplate_gt'].mode().iloc[0]
        mapping[parsed_template] = most_frequent_gt
        
    diffs = []
    for parsed_t, gt_t in mapping.items():
        L_gen = len(str(parsed_t).split())
        L_oracle = len(str(gt_t).split())
        diffs.append(abs(L_gen - L_oracle))
        
    return np.mean(diffs) if diffs else 0.0
