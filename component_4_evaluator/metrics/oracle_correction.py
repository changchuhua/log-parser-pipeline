import pandas as pd

def sort_csv_by_content_order(file1_df, file2_df, to_file=None, save_sorted=False):
    file1_df_unique = file1_df.drop_duplicates(subset='Content', keep='first')
    merged_df = pd.merge(file2_df[['Content']], file1_df_unique, on='Content', how='left')
    if save_sorted and to_file:
        merged_df.to_csv(to_file, index=False)
    return merged_df

def oracle_correct(df_gt):
    df_gt['Content'] = df_gt['Content'].astype(str).str.strip()
    df_gt['EventTemplate'] = df_gt['EventTemplate'].astype(str).str.replace(r'\s+', ' ', regex=True).str.strip()
    return df_gt
