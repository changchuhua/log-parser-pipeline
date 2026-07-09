"""Precomputed Metric Silhouette Score (PMSS) calculator.

Computes a label-free Silhouette score optimized to O(M^2) complexity by executing
Levenshtein distance calculations on unique templates and indexing back to full logs.
"""

import numpy as np
import Levenshtein
from sklearn.metrics import silhouette_score

def calculate_pmss(df_parsed):
    """Calculates the Precomputed Metric Silhouette Score (PMSS).

    Optimizes computation from O(N^2) to O(M^2) (where M << N) by calculating
    Levenshtein distance matrix only on unique templates and broadcasting
    via NumPy index reconstruction.

    Args:
        df_parsed (pd.DataFrame): Parsed results DataFrame containing EventTemplate.

    Returns:
        float: Computed Silhouette score between -1.0 and 1.0.
    """
    templates = df_parsed['EventTemplate'].astype(str).tolist()
    
    # Safety sampling cap: If N is huge, PMSS O(N^2) space complexity causes OOM.
    # We sample up to 10,000 logs randomly (reproducible with fixed seed) to fit memory.
    if len(templates) > 10000:
        import random
        rng = random.Random(42)
        templates = rng.sample(templates, 10000)

    if len(templates) <= 1:
        return 0.0

        
    unique_templates, inverse_indices = np.unique(templates, return_inverse=True)
    m = len(unique_templates)
    
    if m <= 1 or m >= len(templates):
        return 0.0
        
    d_unique = np.zeros((m, m))
    for i in range(m):
        for j in range(i + 1, m):
            dist = Levenshtein.distance(unique_templates[i], unique_templates[j])
            max_len = max(len(unique_templates[i]), len(unique_templates[j]), 1)
            norm_dist = dist / max_len
            d_unique[i, j] = norm_dist
            d_unique[j, i] = norm_dist
            
    d_full = d_unique[inverse_indices, :][:, inverse_indices]
    
    labels = inverse_indices
    score = silhouette_score(d_full, labels, metric='precomputed')
    return score
