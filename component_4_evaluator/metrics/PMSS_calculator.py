"""Parser Medoid Silhouette Score.

It uses medoid silhouette analysis combined with Levenshtein (edit) distance. For every raw log message, it calculates
Cohesion: How structurally similar the raw log is to its assigned parsed template (the "medoid").
Separation: How distinct that raw log is from the nearest different template.
"""

import numpy as np
import pandas as pd
import Levenshtein

def calculate_pmss(df_parsed):
    """Calculates the Parser Medoid Silhouette Score (PMSS).

    Computes the silhouette score natively in O(N * K) time by using the 
    generated templates as cluster medoids and evaluating them against the raw logs.

    Args:
        df_parsed (pd.DataFrame): DataFrame containing 'Content' (raw log) 
                                  and 'EventTemplate' (parsed template).

    Returns:
        float: Computed PMSS score between -1.0 and 1.0.
    """
    if df_parsed.empty or len(df_parsed) <= 1:
        return 0.0

    # PMSS requires BOTH the raw data points and the medoids (templates)
    raw_logs = df_parsed['Content'].astype(str).values
    assigned_templates = df_parsed['EventTemplate'].astype(str).values
    
    unique_templates = np.unique(assigned_templates)
    
    # If the parser lumped everything into 1 single template, separation is impossible
    if len(unique_templates) <= 1:
        return 0.0

    scores = []
    
    for i in range(len(raw_logs)):
        raw_log = raw_logs[i]
        own_template = assigned_templates[i]
        
        # 1. COHESION (a_i): Normalized distance from raw log to its assigned template
        max_len_a = max(len(raw_log), len(own_template), 1)
        a_i = Levenshtein.distance(raw_log, own_template) / max_len_a
        
        # 2. SEPARATION (b_i): Distance from raw log to the nearest *other* template
        b_i = float('inf')
        for temp in unique_templates:
            if temp == own_template:
                continue
            max_len_b = max(len(raw_log), len(temp), 1)
            dist_b = Levenshtein.distance(raw_log, temp) / max_len_b
            if dist_b < b_i:
                b_i = dist_b
                
        # 3. Calculate Silhouette for this specific log line
        if a_i == 0 and b_i == 0:
            s_i = 0.0
        else:
            s_i = (b_i - a_i) / max(a_i, b_i)
            
        scores.append(s_i)
        
    # The final PMSS is the average silhouette score across all logs
    return float(np.mean(scores))