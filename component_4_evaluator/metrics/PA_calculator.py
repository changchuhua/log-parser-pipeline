"""Parsing Accuracy (PA) metric calculator.

Computes the percentage of log lines whose templates were parsed with 100%
correct variable/static token masking compared to ground truth templates.
"""

import pandas as pd
import regex as re

def post_process_tokens(tokens, punc):
    """Normalizes tokens by cleaning punctuation and retaining placeholders.

    Args:
        tokens (list): List of split log message tokens.
        punc (str): String containing punctuation characters to clean.

    Returns:
        list: Normalized tokens list.
    """
    excluded_str = ['=', '|', '(', ')']
    for i in range(len(tokens)):
        if tokens[i].find("<*>") != -1:
            tokens[i] = "<*>"
        else:
            new_str = ""
            for s in tokens[i]:
                if (s not in punc and s != ' ') or s in excluded_str:
                    new_str += s
            tokens[i] = new_str
    return tokens

def message_split(message):
    """Splits a log message template into normalized tokens.

    Args:
        message (str): Log template content.

    Returns:
        list: Filtered and normalized list of token strings.
    """
    punc = r"!\"#$%&'()+,-/:;=?@.[\]^_`{|}~"
    splitters = r"\s\\" + r"\\".join(punc)
    splitter_regex = re.compile("([{}]+)".format(splitters))
    tokens = re.split(splitter_regex, message)
    tokens = list(filter(lambda x: x != "", tokens))
    
    tokens = post_process_tokens(tokens, punc)
    tokens = [
        token.strip()
        for token in tokens
        if token != "" and token != ' ' 
    ]
    tokens = [
        token
        for idx, token in enumerate(tokens)
        if not (token == "<*>" and idx > 0 and tokens[idx - 1] == "<*>")
    ]
    return tokens

def calculate_similarity(template1, template2):
    """Calculates intersection over union similarity of two templates.

    Args:
        template1 (str): First template.
        template2 (str): Second template.

    Returns:
        float: Similarity score between 0.0 and 1.0.
    """
    template1 = message_split(template1)
    template2 = message_split(template2)
    intersection = len(set(template1).intersection(set(template2)))
    union = (len(template1) + len(template2)) - intersection
    return intersection / union

def calculate_parsing_accuracy(groundtruth_df, parsedresult_df, filter_templates=None):
    """Calculates the overall Parsing Accuracy (PA).

    Compares EventTemplate columns of both DataFrames.

    Args:
        groundtruth_df (pd.DataFrame): Ground truth DataFrame.
        parsedresult_df (pd.DataFrame): Parsed results DataFrame.
        filter_templates (set, optional): Optional set of EventTemplates to limit evaluation to.

    Returns:
        float: The computed Parsing Accuracy ratio (0.0 to 1.0).
    """
    if filter_templates is not None:
        groundtruth_df = groundtruth_df[groundtruth_df['EventTemplate'].isin(filter_templates)]
        parsedresult_df = parsedresult_df.loc[groundtruth_df.index]
    correctly_parsed_messages = parsedresult_df[['EventTemplate']].eq(groundtruth_df[['EventTemplate']]).values.sum()
    total_messages = len(parsedresult_df[['Content']])

    PA = float(correctly_parsed_messages) / total_messages
    return PA
