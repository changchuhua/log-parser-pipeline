"""API Client for interfacing with Ollama or local LLM instances.

Provides chat completions and embedding generation with automatic request
retries and support for multiple API response schemas (OpenAI and Ollama direct).
"""

import requests
import yaml
import logging
import os

logger = logging.getLogger(__name__)

class OllamaClient:
    """Client for querying an Ollama or OpenAI-compatible LLM endpoint."""

    def __init__(self, config_path='/app/config.yaml'):
        """Initializes the OllamaClient with config parameters or env variables.

        Args:
            config_path (str): Path to the centralized YAML configuration.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}
            
        self.base_url = os.environ.get('OLLAMA_API_BASE') or config.get('llm', {}).get('base_url', 'http://host.docker.internal:11434/v1')
        self.model_name = config.get('llm', {}).get('model_name', 'llama3')
        self.embedding_model = config.get('logparser_llm', {}).get('embedding_model', 'nomic-embed-text')
        
    def get_embedding(self, text):
        """Generates embedding vector for a given text.

        Retries the request once in case of a timeout or server error.

        Args:
            text (str): The input log message text.

        Returns:
            list: Float vector embeddings from the model.

        Raises:
            requests.exceptions.RequestException: If the request fails twice.
            KeyError: If the expected keys are missing from the response JSON.
        """
        url = f"{self.base_url}/embeddings"
        payload = {
            "model": self.embedding_model,
            "input": text
        }
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if 'data' in data and len(data['data']) > 0:
                    return data['data'][0]['embedding']
                elif 'embedding' in data:
                    return data['embedding']
                else:
                    raise KeyError("No embedding key found in response")
            except requests.exceptions.RequestException as e:
                logger.warning(f"Embedding request attempt {attempt + 1} failed: {e}")
                if attempt == 1:
                    raise e
        
    def generate_completion(self, prompt, temperature=0.0):
        """Generates a text completion for a given prompt.

        Retries the request once in case of a timeout or server error.

        Args:
            prompt (str): Prompt to query the model.
            temperature (float): Model temperature setting. Defaults to 0.0.

        Returns:
            str: Completion result text.

        Raises:
            requests.exceptions.RequestException: If the request fails twice.
            KeyError: If response JSON lacks expected completion keys.
        """
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature
        }
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                data = resp.json()
                if 'choices' in data and len(data['choices']) > 0:
                    return data['choices'][0]['message']['content']
                elif 'message' in data and 'content' in data['message']:
                    return data['message']['content']
                else:
                    raise KeyError("No message content found in response")
            except requests.exceptions.RequestException as e:
                logger.warning(f"Completion request attempt {attempt + 1} failed: {e}")
                if attempt == 1:
                    raise e
