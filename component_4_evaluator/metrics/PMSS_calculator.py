import numpy as np
import Levenshtein
from sklearn.metrics import silhouette_score

def calculate_pmss(df_parsed):
    templates = df_parsed['EventTemplate'].astype(str).tolist()
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
