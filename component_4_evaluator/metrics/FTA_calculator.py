"""File Template Accuracy (FTA) metric calculator.

Evaluates the ratio of correctly parsed unique ground truth templates.
A ground truth template is correctly parsed if all of its log message
instances are correctly mapped to the identical template string in the parsed log.
"""

import pandas as pd

def calculate_fta(df_gt_aligned, df_parsed_aligned, filter_templates=None):
    """Calculates the overall File Template Accuracy (FTA).

    Compares EventTemplate columns of both aligned DataFrames at the template level.

    Args:
        df_gt_aligned (pd.DataFrame): Ground truth DataFrame.
        df_parsed_aligned (pd.DataFrame): Parsed results DataFrame.
        filter_templates (set, optional): Optional set of EventTemplates to limit evaluation to.

    Returns:
        float: The computed File Template Accuracy ratio (0.0 to 1.0).
    """
    if df_gt_aligned.empty or df_parsed_aligned.empty:
        return 0.0

    # Align templates by index
    merged = pd.DataFrame({
        'gt_template': df_gt_aligned['EventTemplate'].values,
        'parsed_template': df_parsed_aligned['EventTemplate'].values
    })

    if filter_templates is not None:
        merged = merged[merged['gt_template'].isin(filter_templates)]

    is_correct = (merged['gt_template'] == merged['parsed_template'])
    correct_per_group = is_correct.groupby(merged['gt_template']).all()
    correct_templates = correct_per_group.sum()
    total_templates = len(correct_per_group)

    return float(correct_templates) / total_templates if total_templates > 0 else 0.0

