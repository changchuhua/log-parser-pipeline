"""Parser Medoid Silhouette Score.

It uses medoid silhouette analysis combined with Levenshtein (edit) distance. For every raw log message, it calculates
Cohesion: How structurally similar the raw log is to its assigned parsed template (the "medoid").
Separation: How distinct that raw log is from the nearest different template.
"""

import bisect
import os
from multiprocessing import Pool, cpu_count

import numpy as np
import pandas as pd
import Levenshtein

# Populated once per worker process (via Pool's initializer) so the shared
# template arrays are sent to each process a single time, not re-pickled for
# every row. On Linux's default fork() start method this would be inherited
# automatically anyway, but the explicit initializer works regardless of
# start method.
_worker_templates = None
_worker_lengths = None
_worker_n = None


def _init_worker(sorted_templates, sorted_lengths):
    global _worker_templates, _worker_lengths, _worker_n
    _worker_templates = sorted_templates
    _worker_lengths = sorted_lengths
    _worker_n = len(sorted_templates)


def _score_row(args, sorted_templates=None, sorted_lengths=None, n_templates=None):
    """Computes the silhouette score for a single (raw_log, own_template) pair.

    Falls back to the module-level worker-process state when called via a
    multiprocessing Pool (sorted_templates/sorted_lengths omitted); accepts
    them directly for the single-process path so both call sites share one
    implementation rather than risking two copies drifting apart.
    """
    if sorted_templates is None:
        sorted_templates = _worker_templates
        sorted_lengths = _worker_lengths
        n_templates = _worker_n

    raw_log, own_template = args
    rl = len(raw_log)

    # 1. COHESION (a_i): Normalized distance from raw log to its assigned template
    max_len_a = max(rl, len(own_template), 1)
    a_i = Levenshtein.distance(raw_log, own_template) / max_len_a

    # 2. SEPARATION (b_i): Distance from raw log to the nearest *other* template.
    # Levenshtein distance is always >= the length difference between the two
    # strings, so templates are pre-sorted by length once (by the caller),
    # letting this search expand outward from the raw log's own length and
    # stop as soon as that lower bound can no longer beat the best distance
    # already found. Exact, not approximate — same result as comparing
    # against every template, far fewer of the expensive distance
    # computations actually run.
    #
    # The search is split into two independent one-directional walks (shorter
    # templates going down, longer templates going up) rather than merged
    # into one pass. That's required for correctness, not just style: for
    # templates shorter than the raw log, max_len_b is constant (== rl), so
    # the lower bound grows linearly with distance-from-raw-log-length; for
    # templates longer than the raw log, max_len_b grows with the template
    # itself, so the lower bound is a *different* monotonic function of
    # distance. Both are safe to early-break on their own, but interleaving
    # them by raw length-difference does not preserve a single monotonic
    # ordering across the two (a longer template can have a smaller
    # normalized bound than a shorter one at the same raw distance), which
    # would make an interleaved early-break unsound.
    b_i = float('inf')
    pos = bisect.bisect_left(sorted_lengths, rl)

    # Shorter-or-equal-length templates: max_len_b == rl (constant), so the
    # lower bound (rl - temp_len) / rl grows monotonically as temp_len drops.
    idx = pos - 1
    while idx >= 0:
        temp_len = sorted_lengths[idx]
        lower_bound = (rl - temp_len) / rl if rl else 0.0
        if lower_bound >= b_i:
            break
        temp = sorted_templates[idx]
        if temp != own_template:
            dist_b = Levenshtein.distance(raw_log, temp) / max(rl, temp_len, 1)
            if dist_b < b_i:
                b_i = dist_b
        idx -= 1

    # Longer templates: max_len_b == temp_len (grows with the candidate), so
    # the lower bound (temp_len - rl) / temp_len grows monotonically with temp_len.
    idx = pos
    while idx < n_templates:
        temp_len = sorted_lengths[idx]
        lower_bound = (temp_len - rl) / temp_len if temp_len else 0.0
        if lower_bound >= b_i:
            break
        temp = sorted_templates[idx]
        if temp != own_template:
            dist_b = Levenshtein.distance(raw_log, temp) / max(rl, temp_len, 1)
            if dist_b < b_i:
                b_i = dist_b
        idx += 1

    # 3. Calculate Silhouette for this specific log line
    if a_i == 0 and b_i == 0:
        return 0.0
    return (b_i - a_i) / max(a_i, b_i)


def calculate_pmss(df_parsed):
    """Calculates the Parser Medoid Silhouette Score (PMSS).

    Computes the silhouette score natively in O(N * K) time by using the
    generated templates as cluster medoids and evaluating them against the raw logs,
    pruned via a Levenshtein lower bound and parallelized across CPU cores — each
    row's score is independent of every other row's, so this is embarrassingly
    parallel. Both are exact optimizations: same result as the naive full O(N*K)
    scan, just far less of the expensive Levenshtein work actually runs, and it
    runs on more cores at once.

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

    sorted_templates = sorted(unique_templates, key=len)
    sorted_lengths = [len(t) for t in sorted_templates]

    row_args = list(zip(raw_logs.tolist(), assigned_templates.tolist()))
    n_rows = len(row_args)

    # PMSS_WORKERS lets this be pinned for testing/tuning; otherwise use all
    # available cores, capped so tiny inputs don't pay multiprocessing
    # startup overhead for no benefit.
    env_workers = os.environ.get('PMSS_WORKERS')
    if env_workers:
        n_workers = max(1, int(env_workers))
    else:
        n_workers = min(cpu_count(), max(1, n_rows // 200))

    if n_workers <= 1:
        scores = [
            _score_row(a, sorted_templates, sorted_lengths, len(sorted_templates))
            for a in row_args
        ]
    else:
        chunksize = max(1, n_rows // (n_workers * 8))
        with Pool(
            processes=n_workers,
            initializer=_init_worker,
            initargs=(sorted_templates, sorted_lengths),
        ) as pool:
            scores = pool.map(_score_row, row_args, chunksize=chunksize)

    # The final PMSS is the average silhouette score across all logs
    return float(np.mean(scores))
