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
            
        self.base_url = os.environ.get('OLLAMA_API_BASE')
        if not self.base_url:
            logger.warning("OLLAMA_API_BASE environment variable is not configured!")

        # Resolve model choice dynamically via environment or config fallback
        model_choice = os.environ.get('OLLAMA_MODEL') or config.get('llm', {}).get('model_name', 'llama3')
        model_map = {
            'gemma': 'gemma4:26b',
            'deepseek': 'deepseek-r1:32b',
            'qwen': 'qwen3.6:27b',
            'llama3': 'llama3'
        }
        self.model_name = model_map.get(model_choice.lower(), model_choice)
        self.embedding_model = config.get('logparser_llm', {}).get('embedding_model', 'nomic-embed-text')
        
        # Token and invocation tracking metrics
        self.invocations = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.llm_timeouts = 0
        self.failed_invocations = 0
        
        # Read request timeout from environment variable (defaults to 90 seconds)
        self.timeout = int(os.environ.get('OLLAMA_TIMEOUT', '90'))
        
        # In-memory transparent cache for embedding requests
        self.embedding_cache = {}
        
        # Set LLM debug toggles based on environment
        self.llm_debug = os.environ.get('LLM_DEBUG', 'false').lower() == 'true'
        
    def _log_debug(self, event_type, request_payload, response_data=None, error_msg=None):
        """Helper to append a JSON transaction log entry to llm_debug.jsonl if enabled."""
        if not self.llm_debug:
            return
        import json
        import datetime
        
        log_entry = {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "event_type": event_type,
            "model": self.model_name if event_type == "generate" else self.embedding_model,
            "request": request_payload,
            "response": response_data,
            "error": error_msg
        }
        try:
            os.makedirs('data/parsed', exist_ok=True)
            with open('data/parsed/llm_debug.jsonl', 'a', encoding='utf-8') as f:
                f.write(json.dumps(log_entry) + '\n')
        except Exception as e:
            logger.error(f"Failed to write to llm_debug.jsonl: {e}")
        
    def get_usage(self):
        """Returns cumulative usage statistics for tokens and invocations.

        Returns:
            dict: usage metadata.
        """
        return {
            "invocations": self.invocations,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_timeouts": self.llm_timeouts,
            "failed_invocations": self.failed_invocations
        }
        
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
        if text in self.embedding_cache:
            return self.embedding_cache[text]

        url = f"{self.base_url}/embeddings"
        payload = {
            "model": self.embedding_model,
            "input": text
        }
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=300)
                resp.raise_for_status()
                data = resp.json()
                
                # Accumulate token usage if present
                usage = data.get('usage', {})
                self.prompt_tokens += usage.get('prompt_tokens', 0)
                self.total_tokens += usage.get('total_tokens', 0)
                
                if 'data' in data and len(data['data']) > 0:
                    emb = data['data'][0]['embedding']
                    self.embedding_cache[text] = emb
                    self._log_debug("embeddings", payload, response_data={"embedding_dim": len(emb)})
                    return emb
                elif 'embedding' in data:
                    emb = data['embedding']
                    self.embedding_cache[text] = emb
                    self._log_debug("embeddings", payload, response_data={"embedding_dim": len(emb)})
                    return emb
                else:
                    raise KeyError("No embedding key found in response")
            except requests.exceptions.RequestException as e:
                if isinstance(e, requests.exceptions.Timeout):
                    self.llm_timeouts += 1
                else:
                    self.failed_invocations += 1
                logger.warning(f"Embedding request attempt {attempt + 1} failed: {e}")
                self._log_debug("embeddings", payload, error_msg=str(e))
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
        base_url_clean = self.base_url or ""
        if base_url_clean.endswith('/v1'):
            base_url_clean = base_url_clean[:-3]
        url = f"{base_url_clean}/api/generate"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "think": False,
            "options": {
                "temperature": temperature,
                "num_predict": 128
            }
        }
        for attempt in range(2):
            try:
                resp = requests.post(url, json=payload, timeout=300)
                resp.raise_for_status()
                data = resp.json()
                
                # Accumulate token usage if present
                p_tokens = data.get('prompt_eval_count', 0)
                c_tokens = data.get('eval_count', 0)
                self.prompt_tokens += p_tokens
                self.completion_tokens += c_tokens
                self.total_tokens += p_tokens + c_tokens
                self.invocations += 1
                
                response_text = None
                if 'response' in data:
                    response_text = data['response']
                elif 'choices' in data and len(data['choices']) > 0:
                    response_text = data['choices'][0]['message']['content']
                elif 'message' in data and 'content' in data['message']:
                    response_text = data['message']['content']
                
                if response_text is not None:
                    self._log_debug("generate", payload, response_data={"response": response_text, "eval_count": data.get('eval_count', 0)})
                    return response_text
                else:
                    raise KeyError("No expected content keys found in response")
            except requests.exceptions.RequestException as e:
                if isinstance(e, requests.exceptions.Timeout):
                    self.llm_timeouts += 1
                else:
                    self.failed_invocations += 1
                logger.warning(f"Completion request attempt {attempt + 1} failed: {e}")
                self._log_debug("generate", payload, error_msg=str(e))
                if attempt == 1:
                    raise e
