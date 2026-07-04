"""Few-shot LLM parsing module with self-reflection.

Formats prompts with retrieved memory examples and performs multi-turn query loops
to generalized dynamic variables.
"""

import logging
from core.llm_client import OllamaClient

logger = logging.getLogger(__name__)

class LlamaParser:
    """Invokes chat completions to parse logs and refine results via self-reflection."""

    def __init__(self, llm_client=None, enable_reflection=True):
        """Initializes LlamaParser.

        Args:
            llm_client (OllamaClient, optional): Client instance.
            enable_reflection (bool): If True, queries LLM again to generalize variables.
        """
        if llm_client is None:
            self.llm_client = OllamaClient()
        else:
            self.llm_client = llm_client
        self.enable_reflection = enable_reflection

    def parse_log(self, log_text, examples):
        """Builds prompt, queries model, and cleans template.

        Args:
            log_text (str): Log message.
            examples (list): Top k similar log examples.

        Returns:
            str: Refined template.
        """
        prompt = "You are a log parser.\n\n"
        if examples:
            prompt += "Provided Examples:\n"
            for i, ex in enumerate(examples):
                prompt += f"Log: {ex['raw_log']}\nTemplate: {ex['template']}\n\n"
                
        prompt += f"Compare the following log to the provided examples and extract its static template by replacing varying dynamic parameters with <*>. Log: {log_text}\nTemplate:"

        try:
            generated_template = self.llm_client.generate_completion(prompt).strip()
            generated_template = self.clean_template(generated_template)
            
            if self.enable_reflection:
                reflection_prompt = (
                    f"You previously parsed this log: '{log_text}' into this template: '{generated_template}'. "
                    "Review your template carefully. Did you leave any dynamic variables, IDs, or specific numbers unmasked? "
                    "If so, replace them with <*>. Output ONLY the highly generalized, refined template."
                )
                refined_template = self.llm_client.generate_completion(reflection_prompt).strip()
                refined_template = self.clean_template(refined_template)
                return refined_template
                
            return generated_template
            
        except Exception as e:
            logger.error(f"LlamaParser error: {e}")
            return log_text

    def clean_template(self, template):
        """Removes code blocks and markdown wrapping from the template output.

        Args:
            template (str): Raw template string.

        Returns:
            str: Sanitized template.
        """
        if not template:
            return ""
        t = template.strip()
        if t.startswith("```"):
            t = t.split('\n', 1)[-1]
            if t.endswith("```"):
                t = t.rsplit('\n', 1)[0]
        return t.replace('`', '').strip()
