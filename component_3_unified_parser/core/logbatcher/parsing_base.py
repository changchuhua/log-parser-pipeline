"""Zero-shot batch template prompt formatting and querying for LogBatcher.

Formats diverse log batches into zero-shot template extraction prompts and resolves
them using the Ollama completion backend.
"""

import logging
from core.llm_client import OllamaClient

logger = logging.getLogger(__name__)

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

    def batch_query(self, batch_logs):
        """Queries the LLM with a batch of diverse logs to discover their static template.

        Args:
            batch_logs (list): List of log dictionaries representing a diverse batch.

        Returns:
            str: Predicted static template if successful, else None.
        """
        prompt = (
            "Here is a batch of diverse logs from the same system. They share the same static template "
            "but contain different dynamic variables. Identify the static template they share by replacing "
            "the varying parameters with the placeholder <*>. Output ONLY the final template string.\n\n"
        )
        for i, log in enumerate(batch_logs):
            prompt += f"Log {i+1}: {log.get('message', '')}\n"

        try:
            result = self.llm_client.generate_completion(prompt).strip()
            return result
        except Exception as e:
            logger.error(f"ParsingBase batch query failed: {e}")
            return None
