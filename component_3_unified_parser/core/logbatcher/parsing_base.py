"""Zero-shot batch template prompt formatting and querying for LogBatcher.

Formats diverse log batches into zero-shot template extraction prompts and resolves
them using the Ollama completion backend.
"""

import logging
import re
from core.llm_client import OllamaClient

logger = logging.getLogger(__name__)

# Upstream's own 15-type variable taxonomy (logbatcher/parser.py::get_responce()),
# used verbatim by prompt_mode="original" below.
_ORIGINAL_VARIABLE_TYPES = [
    'url', 'IPv4_port', 'host_port', 'package_host', 'IPv6', 'Mac_address',
    'time', 'path', 'id', 'date', 'duration', 'size', 'numerical',
    'weekday_months', 'user_name'
]

class ParsingBase:
    """Formats batch logs and executes zero-shot queries on LLM clients."""

    def __init__(self, llm_client=None):
        """Initializes ParsingBase.

        Args:
            llm_client (OllamaClient, optional): LLM Client reference.
        """
        if llm_client is None:
            self.llm_client = OllamaClient()
        else:
            self.llm_client = llm_client

    def batch_query(self, batch_logs, prompt_mode="production", historical_variables=None):
        """Queries the LLM with a batch of diverse logs to discover their static template.

        Args:
            batch_logs (list): List of log dictionaries representing a diverse batch.
            prompt_mode (str): "production" (default) uses our own worked-example
                prompt with direct <*> output. "original" faithfully ports
                upstream's zero-shot, 15-type-taxonomy prompt with historical-
                variables grounding and {{placeholder}}/${...} output
                convention -- converted back to <*> before returning, so
                callers always get a <*>-normalized template either way.
            historical_variables (list, optional): Only used when
                prompt_mode="original" -- upstream's `variable_candidates`
                equivalent, included in the prompt as few-shot-style grounding.

        Returns:
            str: Predicted static template if successful, else None.
        """
        if prompt_mode == "original":
            messages = self._build_original_prompt(batch_logs, historical_variables or [])
        else:
            messages = self._build_production_prompt(batch_logs)

        try:
            result = self.llm_client.generate_completion(messages).strip()
        except Exception as e:
            logger.error(f"ParsingBase batch query failed: {e}")
            return None

        if prompt_mode == "original":
            result = self._extract_original_response(result)
            result = self._convert_original_placeholders(result)
        return result

    def _build_production_prompt(self, batch_logs):
        system_instr = (
            "You are an expert log parser. Your task is to identify the static template shared by a batch of logs.\n"
            "Analyze the logs, identify the dynamic variables, and replace them with the placeholder <*>.\n"
            "CRITICAL: Do NOT include any markdown code blocks, introductory text, conversational preamble, or explanation. Output ONLY the raw template string itself.\n\n"
            "Example logs:\n"
            "Log 1: Connection from 192.168.1.5 closed by port 22\n"
            "Log 2: Connection from 10.0.0.12 closed by port 22\n"
            "Example Output:\n"
            "Connection from <*> closed by port 22"
        )
        user_content = "Now parse the following logs:\n"
        for i, log in enumerate(batch_logs):
            user_content += f"Log {i+1}: {log.get('message', '')}\n"

        return [
            {"role": "system", "content": system_instr},
            {"role": "user", "content": user_content}
        ]

    def _build_original_prompt(self, batch_logs, historical_variables):
        """Faithful port of upstream's get_responce() instruction/user-message
        construction (logbatcher/parser.py), with one deliberate, disclosed
        adaptation: an explicit anti-preamble instruction appended at the end.

        Upstream's own instruction text has no such line -- it didn't need
        one against GPT-4o-mini, which upstream was written against and which
        apparently follows "delimited by backticks" tersely on its own. Our
        local model doesn't: a live run against real logs showed it routinely
        prefacing its answer with conversational explanation before the
        backtick block (e.g. "The log messages contain a consistent
        structure..."), which upstream's own extraction heuristic (longest
        backtick-delimited segment -- see _extract_original_response) isn't
        always robust against once there's substantial prose in the response.
        This instruction targets the model's behavior, not upstream's
        algorithm -- the taxonomy, {{}} format, and historical-variables
        grounding above are unchanged.
        """
        variable_prompt = f' Historical variables: {historical_variables}.' if historical_variables else ''
        instruction = (
            "You will be provided with some log messages separated by line break. "
            "You must abstract variables with `{{placeholders}}` to extract the corresponding template. "
            f"The variable type in log messages can be any of the following: {_ORIGINAL_VARIABLE_TYPES}."
            + variable_prompt +
            " Constant text and strings should not be recognized as variables.\n"
            "Print the input log's template delimited by backticks.\n"
            "Do not include any explanation, reasoning, or conversational preamble -- output only the backtick-delimited template."
        )
        user_content = '\n'.join(
            f"Log[{i+1}]: `{log.get('message', '')}`" for i, log in enumerate(batch_logs)
        )
        return [
            {"role": "system", "content": instruction},
            {"role": "user", "content": user_content}
        ]

    @staticmethod
    def _extract_original_response(response):
        """Faithful port of upstream's post_process()'s backtick-boundary
        extraction (the half of post_process() that runs before {{}}/${}
        conversion and correct_single_template()).

        Unlike our own clean_template() (which only strips a leading/trailing
        ``` fence), this finds the first and last backtick anywhere in the
        response, extracts everything between them, splits on internal
        backticks, discards degenerate segments (empty once <*>/spaces are
        stripped), and keeps the longest remaining one. Needed because local
        models (unlike the GPT-4o-mini upstream was written against) often
        add conversational preamble before the backtick-delimited answer even
        though the prompt doesn't explicitly forbid it -- clean_template()'s
        simpler leading-fence check leaves that preamble attached to the
        template, silently breaking every downstream match. Confirmed via a
        live run against real logs, not a hypothetical.

        Deliberately preserves upstream's own list-mutate-while-iterating
        quirk (removing from tmps while iterating over it, which can skip an
        element) -- faithful means faithful to actual behavior, not an
        idealized fix.
        """
        response = response.replace('\n', '')
        first_backtick_index = response.find('`')
        last_backtick_index = response.rfind('`')
        if first_backtick_index == -1 or last_backtick_index == -1 or first_backtick_index == last_backtick_index:
            tmps = []
        else:
            tmps = response[first_backtick_index: last_backtick_index + 1].split('`')
        for tmp in tmps:
            if tmp.replace(' ', '').replace('<*>', '') == '':
                tmps.remove(tmp)
        if len(tmps) == 1:
            return tmps[0]
        if len(tmps) > 1:
            return max(tmps, key=len)
        return ''

    @staticmethod
    def _convert_original_placeholders(response):
        """Upstream's post_process()'s {{...}}/${...} -> <*> conversion,
        applied so callers get a <*>-normalized template regardless of
        prompt_mode -- keeps postprocess_mode fully decoupled from prompt_mode."""
        template = re.sub(r'\{\{.*?\}\}', '<*>', response)
        template = re.sub(r'\$\{.*?\}', '<*>', template)
        return template
