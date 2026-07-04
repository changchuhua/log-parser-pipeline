"""Levenshtein Edit Distance (ED) metric calculator.

Calculates character-level differences and normalized similarities between ground
truth templates and parsed templates using Levenshtein distance.
"""

from tqdm import tqdm
import Levenshtein

def calculate_edit_distance(groundtruth, parsedresult):
    """Calculates average Edit Distance (ED) and Normalized Edit Distance (NED).

    Utilizes caching to optimize performance on identical comparisons.

    Args:
        groundtruth (pd.DataFrame): Ground truth DataFrame containing EventTemplate.
        parsedresult (pd.DataFrame): Parsed results DataFrame containing EventTemplate.

    Returns:
        tuple: (accuracy_ED, accuracy_NED) floats.
    """
    edit_distance_result, normalized_ed_result, cache_dict = [], [] , {}
    iterable = zip(groundtruth['EventTemplate'].values, parsedresult['EventTemplate'].values)
    length_logs = len(groundtruth['EventTemplate'].values)
    iterable = tqdm(iterable, total=length_logs)
    for i, j in iterable:
        i_str = str(i)
        j_str = str(j)
        if i_str != j_str:
            if (i_str, j_str) in cache_dict:
                ed = cache_dict[(i_str, j_str)]
            else:
                ed = Levenshtein.distance(i_str, j_str)
                cache_dict[(i_str, j_str)] = ed
            normalized_ed = 1 - ed / max(len(i_str), len(j_str), 1)
            edit_distance_result.append(ed)
            normalized_ed_result.append(normalized_ed)
        else:
            edit_distance_result.append(0)
            normalized_ed_result.append(1.0)

    accuracy_ED = sum(edit_distance_result) / length_logs if length_logs > 0 else 0.0
    accuracy_NED = sum(normalized_ed_result) / length_logs if length_logs > 0 else 0.0
    return accuracy_ED, accuracy_NED