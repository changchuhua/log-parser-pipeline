"""Grouping Accuracy (GA) metric calculator.

Evaluates how accurately the log parser partitions log messages into clusters
representing templates compared to ground truth groupings.
"""

import sys
import pandas as pd
from collections import defaultdict
from scipy.special import comb
from tqdm import tqdm

def evaluate(df_groundtruth, df_parsedlog, filter_templates=None):
    """Aligns DataFrames and calculates grouping accuracies.

    Filters out any null ground truth templates and computes Grouping Accuracy
    and F-Grouping Accuracy.

    Args:
        df_groundtruth (pd.DataFrame): Ground truth DataFrame.
        df_parsedlog (pd.DataFrame): Parsed outputs DataFrame.
        filter_templates (set, optional): Optional set of templates to restrict evaluation to.

    Returns:
        tuple: (GA, FGA) floats representing accuracy and F1 grouping accuracy.
    """
    null_logids = df_groundtruth[~df_groundtruth['EventTemplate'].isnull()].index
    df_groundtruth = df_groundtruth.loc[null_logids]
    df_parsedlog = df_parsedlog.loc[null_logids]
    (GA, FGA) = get_accuracy(df_groundtruth['EventTemplate'], df_parsedlog['EventTemplate'], filter_templates)
    return GA, FGA

def get_accuracy(series_groundtruth, series_parsedlog, filter_templates=None):
    """Calculates GA and FGA accuracy values from ground truth and parsed templates.

    Args:
        series_groundtruth (pd.Series): Ground truth EventTemplate column values.
        series_parsedlog (pd.Series): Parsed EventTemplate column values.
        filter_templates (set, optional): Optional template filters.

    Returns:
        tuple: (GA, FGA) floats representing standard Grouping Accuracy and F1 Grouping Accuracy.
    """
    series_groundtruth_valuecounts = series_groundtruth.value_counts()
    series_parsedlog_valuecounts = series_parsedlog.value_counts()
    df_combined = pd.concat([series_groundtruth, series_parsedlog], axis=1, keys=['groundtruth', 'parsedlog'])
    grouped_df = df_combined.groupby('groundtruth')
    accurate_events = 0 
    accurate_templates = 0
    if filter_templates is not None:
        filter_identify_templates = set()
    for ground_truthId, group in grouped_df:
        series_parsedlog_logId_valuecounts = group['parsedlog'].value_counts()
        if filter_templates is not None and ground_truthId in filter_templates:
            for parsed_eventId in series_parsedlog_logId_valuecounts.index:
                filter_identify_templates.add(parsed_eventId)
        if series_parsedlog_logId_valuecounts.size == 1:
            parsed_eventId = series_parsedlog_logId_valuecounts.index[0]
            if len(group) == series_parsedlog_valuecounts[parsed_eventId]:
                if (filter_templates is None) or (ground_truthId in filter_templates):
                    accurate_events += len(group)
                    accurate_templates += 1
    if filter_templates is not None:
        GA = float(accurate_events) / len(series_groundtruth[series_groundtruth.isin(filter_templates)])
        PGA = float(accurate_templates) / len(filter_identify_templates)
        RGA = float(accurate_templates) / len(filter_templates)
    else:
        GA = float(accurate_events) / len(series_groundtruth)
        PGA = float(accurate_templates) / len(series_parsedlog_valuecounts)
        RGA = float(accurate_templates) / len(series_groundtruth_valuecounts)
    FGA = 0.0
    if PGA != 0 or RGA != 0:
        FGA = 2 * (PGA * RGA) / (PGA + RGA)
    return GA, FGA