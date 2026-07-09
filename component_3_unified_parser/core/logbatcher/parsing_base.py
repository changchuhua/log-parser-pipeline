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
            "You are an expert log parser. Your task is to identify the static template shared by a batch of logs.\n"
            "Analyze the logs, identify the dynamic variables, and replace them with the placeholder <*>.\n"
            "CRITICAL: Do NOT include any markdown code blocks, introductory text, conversational preamble, or explanation. Output ONLY the raw template string itself.\n\n"
            "Example logs:\n"
            "Log 1: Connection from 192.168.1.5 closed by port 22\n"
            "Log 2: Connection from 10.0.0.12 closed by port 22\n"
            "Example Output:\n"
            "Connection from <*> closed by port 22\n\n"
            "Now parse the following logs:\n"
        )
        for i, log in enumerate(batch_logs):
            prompt += f"Log {i+1}: {log.get('message', '')}\n"

        try:
            result = self.llm_client.generate_completion(prompt).strip()
            return result
        except Exception as e:
            logger.error(f"ParsingBase batch query failed: {e}")
            return None
