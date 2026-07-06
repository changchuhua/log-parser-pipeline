# Component 3: Parser Prompts Guide

This document details the exact LLM prompt templates used by the three parsing methodologies implemented within Component 3 of the `log-parser-pipeline`.

---

## 1. LogParser-LLM Prompt Template

* **Module**: `core/logparser_llm/llm_extractor.py`
* **Task Type**: In-Context Learning (ICL) using specific semantic category replacement tokens.

### Prompt Structure
```text
As a log parser, your task is to analyze logs and identify dynamic variables. The categories are: Object ID (<OID>), Location Indicator (<LOC>), Object Name (<OBJ>), Type Indicator (<TYP>), Switch Indicator (<SWI>), Time/Duration (<TIM>), Computing Resources (<RES>), Object Amount (<AMT>), Status Code (<STA>), and Other Parameters (<OTH>). Substitute dynamic variables with their respective category tokens. Everything outside the tokens should remain exactly unchanged. Do not fix typos. If a variable is a compound string, replace the entire compound with a single token.

[Dynamic K-Shot Examples from Seed Pool]:
Log: {example_log_1}
Template: {example_template_1}

Log: {example_log_2}
Template: {example_template_2}

Log: {target_log_message}
Template:
```

---

## 2. LogBatcher Prompt Template

* **Module**: `core/logbatcher/parsing_base.py`
* **Task Type**: Zero-Shot Batch Invariant Template Extraction.

### Prompt Structure
```text
Here is a batch of diverse logs from the same system. They share the same static template but contain different dynamic variables. Identify the static template they share by replacing the varying parameters with the placeholder <*>. Output ONLY the final template string.

Log 1: {log_message_1}
Log 2: {log_message_2}
Log 3: {log_message_3}
...
```

---

## 3. LibreLog Prompt Templates

* **Module**: `core/librelog/llama_parser.py`
* **Task Type**: Two-step (Few-Shot Parsing followed by Self-Reflection/Self-Correction).

### Prompt 1: Initial Parsing
```text
You are a log parser.

[Provided Examples, if memory contains matching group keys]:
Log: {example_raw_log_1}
Template: {example_template_1}

Log: {example_raw_log_2}
Template: {example_template_2}

Compare the following log to the provided examples and extract its static template by replacing varying dynamic parameters with <*>. Log: {target_log_text}
Template:
```

### Prompt 2: Self-Reflection
```text
You previously parsed this log: '{target_log_text}' into this template: '{initial_generated_template}'. Review your template carefully. Did you leave any dynamic variables, IDs, or specific numbers unmasked? If so, replace them with <*>. Output ONLY the highly generalized, refined template.
```
