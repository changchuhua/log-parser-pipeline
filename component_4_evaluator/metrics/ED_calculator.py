from tqdm import tqdm
import Levenshtein

def calculate_edit_distance(groundtruth, parsedresult):
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