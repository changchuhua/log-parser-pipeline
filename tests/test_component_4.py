import unittest
import pandas as pd
import numpy as np

from component_4_evaluator.metrics.GA_calculator import evaluate as calculate_ga
from component_4_evaluator.metrics.PA_calculator import calculate_parsing_accuracy as calculate_pa
from component_4_evaluator.metrics.ED_calculator import calculate_edit_distance as calculate_ed
from component_4_evaluator.metrics.GD_calculator import calculate_ggd, calculate_pgd
from component_4_evaluator.metrics.oracle_correction import oracle_correct

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
        self.assertAlmostEqual(ed_score, 2.0 / 3.0)

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

    def test_oracle_correction(self):
        df_dirty = pd.DataFrame([
            {'LineId': '1', 'Content': 'User 1 logged in  ', 'EventTemplate': 'User   <*> logged in\t'}
        ])
        df_clean = oracle_correct(df_dirty)
        self.assertEqual(df_clean.iloc[0]['Content'], 'User 1 logged in')
        self.assertEqual(df_clean.iloc[0]['EventTemplate'], 'User <*> logged in')
