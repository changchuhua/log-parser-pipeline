import pandas as pd
import regex as re

def post_process_tokens(tokens, punc):
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
    punc = "!\"#$%&'()+,-/:;=?@.[\]^_`{|}~"
    splitters = "\s\\" + "\\".join(punc)
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
    template1 = message_split(template1)
    template2 = message_split(template2)
    intersection = len(set(template1).intersection(set(template2)))
    union = (len(template1) + len(template2)) - intersection
    return intersection / union

def calculate_parsing_accuracy(groundtruth_df, parsedresult_df, filter_templates=None):
    if filter_templates is not None:
        groundtruth_df = groundtruth_df[groundtruth_df['EventTemplate'].isin(filter_templates)]
        parsedresult_df = parsedresult_df.loc[groundtruth_df.index]
    correctly_parsed_messages = parsedresult_df[['EventTemplate']].eq(groundtruth_df[['EventTemplate']]).values.sum()
    total_messages = len(parsedresult_df[['Content']])

    PA = float(correctly_parsed_messages) / total_messages
    return PA
