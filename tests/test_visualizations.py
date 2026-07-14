import pandas as pd
from component_4_evaluator.metrics.FTA_calculator import calculate_fta
from component_4_evaluator.evaluate_metrics import apply_sensitivity_correction

def test_calculate_fta():
    # Ground truth templates
    df_gt = pd.DataFrame({
        'LineId': ['1', '2', '3', '4'],
        'EventTemplate': ['template A', 'template A', 'template B', 'template B']
    })
    
    # All parsed logs correctly map
    df_parsed_correct = pd.DataFrame({
        'LineId': ['1', '2', '3', '4'],
        'EventTemplate': ['template A', 'template A', 'template B', 'template B']
    })
    
    # 1 correct template (B), 1 wrong (A matches but one line differs)
    df_parsed_partial = pd.DataFrame({
        'LineId': ['1', '2', '3', '4'],
        'EventTemplate': ['template A', 'different A', 'template B', 'template B']
    })
    
    assert calculate_fta(df_gt, df_parsed_correct) == 1.0
    assert calculate_fta(df_gt, df_parsed_partial) == 0.8

def test_apply_sensitivity_correction():
    df_gt = pd.DataFrame({
        'EventTemplate': ['Template  A.', 'Template B:']
    })
    df_parsed = pd.DataFrame({
        'EventTemplate': ['template a', 'Template B']
    })
    
    # spaced correction collapses multiple spaces
    gt_sp, parsed_sp = apply_sensitivity_correction(df_gt, df_parsed, 'spaced')
    assert gt_sp.loc[0, 'EventTemplate'] == 'Template A.'
    assert parsed_sp.loc[0, 'EventTemplate'] == 'template a'
    
    # lowercase correction standardizes to lowercase
    gt_lc, parsed_lc = apply_sensitivity_correction(df_gt, df_parsed, 'lowercase')
    assert gt_lc.loc[0, 'EventTemplate'] == 'template a.'
    assert parsed_lc.loc[0, 'EventTemplate'] == 'template a'
    
    # regex_clean strips trailing symbols
    gt_rc, parsed_rc = apply_sensitivity_correction(df_gt, df_parsed, 'regex_clean')
    assert gt_rc.loc[0, 'EventTemplate'] == 'template a'
    assert parsed_rc.loc[0, 'EventTemplate'] == 'template a'
    assert gt_rc.loc[1, 'EventTemplate'] == 'template b'
    assert parsed_rc.loc[1, 'EventTemplate'] == 'template b'
