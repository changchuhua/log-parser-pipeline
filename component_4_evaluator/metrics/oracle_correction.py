"""Oracle alignment and template correction helpers.

Provides sorting utilities based on raw log content order and regex normalizations
to align parsed templates and ground truths.
"""

import pandas as pd

def sort_csv_by_content_order(file1_df, file2_df, to_file=None, save_sorted=False):
    """Sorts ground truth logs matching the line order of parsed output.

    Aligns record lines based on the first occurrence of log content values.

    Args:
        file1_df (pd.DataFrame): Ground truth DataFrame to sort.
        file2_df (pd.DataFrame): Parsed output DataFrame containing reference order.
        to_file (str, optional): Target file path to write sorted outputs.
        save_sorted (bool): If True, writes the sorted DataFrame to to_file.

    Returns:
        pd.DataFrame: Sorted and aligned ground truth DataFrame.
    """
    file1_df_unique = file1_df.drop_duplicates(subset='Content', keep='first')
    merged_df = pd.merge(file2_df[['Content']], file1_df_unique, on='Content', how='left')
    if save_sorted and to_file:
        merged_df.to_csv(to_file, index=False)
    return merged_df

def oracle_correct(df_gt):
    """Normalizes template whitespaces in the ground truth DataFrame.

    Cleans double spacing and strips padding from Content and EventTemplate columns.

    Args:
        df_gt (pd.DataFrame): Input ground truth DataFrame.

    Returns:
        pd.DataFrame: Cleaned ground truth DataFrame.
    """
    df_gt['Content'] = df_gt['Content'].astype(str).str.strip()
    df_gt['EventTemplate'] = df_gt['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()
    return df_gt
