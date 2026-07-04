import logging
from core.llm_client import OllamaClient

logger = logging.getLogger(__name__)

class ParsingBase:
    def __init__(self, llm_client=None):
        if llm_client is None:
            self.llm_client = OllamaClient()
        else:
            self.llm_client = llm_client

    def batch_query(self, batch_logs):
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
