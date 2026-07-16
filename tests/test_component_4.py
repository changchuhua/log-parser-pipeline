import os
import unittest
import pandas as pd
import numpy as np

from component_4_evaluator.metrics.GA_calculator import evaluate as calculate_ga
from component_4_evaluator.metrics.PA_calculator import calculate_parsing_accuracy as calculate_pa
from component_4_evaluator.metrics.ED_calculator import calculate_edit_distance as calculate_ed
from component_4_evaluator.metrics.GD_calculator import calculate_ggd, calculate_pgd
from component_4_evaluator.metrics.oracle_correction import oracle_correct
from component_4_evaluator.metrics.PMSS_calculator import calculate_pmss

import Levenshtein


def _naive_pmss(df_parsed):
    """Unoptimized O(N*K) reference PMSS, kept only to cross-check the pruned
    implementation in calculate_pmss() — same algorithm as it was before the
    length-bound pruning optimization was added.
    """
    raw_logs = df_parsed['Content'].astype(str).values
    assigned_templates = df_parsed['EventTemplate'].astype(str).values
    unique_templates = np.unique(assigned_templates)
    if len(unique_templates) <= 1:
        return 0.0
    scores = []
    for i in range(len(raw_logs)):
        raw_log = raw_logs[i]
        own_template = assigned_templates[i]
        max_len_a = max(len(raw_log), len(own_template), 1)
        a_i = Levenshtein.distance(raw_log, own_template) / max_len_a
        b_i = float('inf')
        for temp in unique_templates:
            if temp == own_template:
                continue
            max_len_b = max(len(raw_log), len(temp), 1)
            dist_b = Levenshtein.distance(raw_log, temp) / max_len_b
            if dist_b < b_i:
                b_i = dist_b
        s_i = 0.0 if (a_i == 0 and b_i == 0) else (b_i - a_i) / max(a_i, b_i)
        scores.append(s_i)
    return float(np.mean(scores))

class TestComponent4(unittest.TestCase):
    def setUp(self):
        self.df_gt = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in', 'EventTemplate': 'User <*> logged in'},
            {'LineId': '2', 'Content': 'User 2 logged in', 'EventTemplate': 'User <*> logged in'},
            {'LineId': '3', 'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'Connection failed from <*>'}
        ])
        
    def test_pa_calculator(self):
        df_parsed_exact = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in', 'EventTemplate': 'User <*> logged in'},
            {'LineId': '2', 'Content': 'User 2 logged in', 'EventTemplate': 'User <*> logged in'},
            {'LineId': '3', 'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'Connection failed from <*>'}
        ])
        pa_score = calculate_pa(self.df_gt, df_parsed_exact)
        self.assertEqual(pa_score, 1.0)
        
        df_parsed_mismatch = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in', 'EventTemplate': 'User <*> logged in'},
            {'LineId': '2', 'Content': 'User 2 logged in', 'EventTemplate': 'User 2 logged in'},
            {'LineId': '3', 'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'Connection failed from <*>'}
        ])
        pa_score = calculate_pa(self.df_gt, df_parsed_mismatch)
        self.assertAlmostEqual(pa_score, 2.0 / 3.0)

    def test_ga_calculator(self):
        df_parsed_perfect = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in', 'EventTemplate': 'T1'},
            {'LineId': '2', 'Content': 'User 2 logged in', 'EventTemplate': 'T1'},
            {'LineId': '3', 'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'T2'}
        ])
        ga_score, fga_score = calculate_ga(self.df_gt, df_parsed_perfect)
        self.assertEqual(ga_score, 1.0)

        df_parsed_imperfect = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in', 'EventTemplate': 'T1'},
            {'LineId': '2', 'Content': 'User 2 logged in', 'EventTemplate': 'T2'},
            {'LineId': '3', 'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'T2'}
        ])
        ga_score, fga_score = calculate_ga(self.df_gt, df_parsed_imperfect)
        self.assertEqual(ga_score, 0.0)

    def test_ed_calculator(self):
        df_parsed = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in', 'EventTemplate': 'User 1 logged in'},
            {'LineId': '2', 'Content': 'User 2 logged in', 'EventTemplate': 'User <*> logged in'},
            {'LineId': '3', 'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'Connection failed from <*>'}
        ])
        ed_score, ned_score = calculate_ed(self.df_gt, df_parsed)
        self.assertAlmostEqual(ed_score, 1.0)

    def test_gd_calculator(self):
        df_parsed = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in', 'EventTemplate': 'T1'},
            {'LineId': '2', 'Content': 'User 2 logged in', 'EventTemplate': 'T2'},
            {'LineId': '3', 'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'T3'}
        ])
        ggd_score = calculate_ggd(self.df_gt, df_parsed)
        self.assertEqual(ggd_score, 0.5)

        pgd_score = calculate_pgd(self.df_gt, df_parsed)
        self.assertEqual(pgd_score, 3.0)

    def test_pmss_calculator_basic(self):
        # Single shared template: separation is undefined -> 0.0
        df_one_template = pd.DataFrame([
            {'Content': 'User 1 logged in', 'EventTemplate': 'User <*> logged in'},
            {'Content': 'User 2 logged in', 'EventTemplate': 'User <*> logged in'},
        ])
        self.assertEqual(calculate_pmss(df_one_template), 0.0)

        # Clearly distinct templates should score positively (own template is a
        # much closer match than the unrelated one).
        df_separated = pd.DataFrame([
            {'Content': 'User 1 logged in', 'EventTemplate': 'User <*> logged in'},
            {'Content': 'User 2 logged in', 'EventTemplate': 'User <*> logged in'},
            {'Content': 'Connection failed from 10.0.0.1', 'EventTemplate': 'Connection failed from <*>'},
            {'Content': 'Connection failed from 10.0.0.2', 'EventTemplate': 'Connection failed from <*>'},
        ])
        self.assertGreater(calculate_pmss(df_separated), 0.0)

    def test_pmss_calculator_matches_naive_reference(self):
        # Cross-checks the length-bound-pruned calculate_pmss() against the
        # unoptimized O(N*K) reference it was derived from, across random
        # string-length distributions on both sides of each row's own length
        # (the case the pruning logic has to get right for correctness — see
        # PMSS_calculator.py's comment on why the two walk directions can't be
        # interleaved).
        rng = np.random.default_rng(42)
        alphabet = "abcde <*>"
        for trial in range(5):
            n_templates = rng.integers(3, 8)
            templates = []
            for _ in range(n_templates):
                length = rng.integers(1, 40)
                templates.append(''.join(rng.choice(list(alphabet), size=length)))
            templates = list(dict.fromkeys(templates))  # de-dupe, preserve order
            if len(templates) < 2:
                continue

            n_rows = 30
            rows = []
            for _ in range(n_rows):
                own_template = rng.choice(templates)
                length = rng.integers(1, 40)
                content = ''.join(rng.choice(list(alphabet), size=length))
                rows.append({'Content': content, 'EventTemplate': own_template})
            df = pd.DataFrame(rows)

            expected = _naive_pmss(df)
            actual = calculate_pmss(df)
            self.assertAlmostEqual(
                actual, expected, places=9,
                msg=f"trial {trial}: pruned PMSS diverged from naive reference"
            )

    def test_pmss_calculator_parallel_matches_serial(self):
        # calculate_pmss() auto-picks serial vs. multiprocessing based on row
        # count; force the parallel path via PMSS_WORKERS so it's actually
        # exercised here, and check it agrees with both the serial path and
        # the naive O(N*K) reference on the same data.
        rng = np.random.default_rng(7)
        alphabet = "abcde <*>"
        templates = []
        for _ in range(6):
            length = rng.integers(1, 40)
            templates.append(''.join(rng.choice(list(alphabet), size=length)))
        templates = list(dict.fromkeys(templates))

        rows = []
        for _ in range(120):
            own_template = rng.choice(templates)
            length = rng.integers(1, 40)
            content = ''.join(rng.choice(list(alphabet), size=length))
            rows.append({'Content': content, 'EventTemplate': own_template})
        df = pd.DataFrame(rows)

        expected = _naive_pmss(df)

        old_env = os.environ.get('PMSS_WORKERS')
        try:
            os.environ['PMSS_WORKERS'] = '4'
            parallel_result = calculate_pmss(df)
        finally:
            if old_env is None:
                os.environ.pop('PMSS_WORKERS', None)
            else:
                os.environ['PMSS_WORKERS'] = old_env

        self.assertAlmostEqual(parallel_result, expected, places=9)

    def test_oracle_correction(self):
        df_dirty = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in  ', 'EventTemplate': 'User   <*> logged in\t'}
        ])
        df_clean = oracle_correct(df_dirty)
        self.assertEqual(df_clean.iloc[0]['Content'], 'User 1 logged in')
        self.assertEqual(df_clean.iloc[0]['EventTemplate'], 'User <*> logged in')
