import unittest
from unittest.mock import patch, MagicMock
import requests
from component_3_unified_parser.core.llm_client import OllamaClient

class TestComponent3Client(unittest.TestCase):
    def setUp(self):
        # Instantiate client with a dummy config_path that doesn't exist to trigger default values
        self.client = OllamaClient(config_path='non_existent_config.yaml')

    @patch('requests.post')
    def test_generate_completion(self, mock_post):
        mock_resp = MagicMock()
        # Direct Ollama API format
        mock_resp.json.return_value = {
            "model": "llama3",
            "message": {
                "role": "assistant",
                "content": "Parsed Template <*>"
            }
        }
        mock_post.return_value = mock_resp

        result = self.client.generate_completion("test prompt")
        self.assertEqual(result, "Parsed Template <*>")
        mock_post.assert_called_once()

    @patch('requests.post')
    def test_get_embedding(self, mock_post):
        mock_resp = MagicMock()
        # Direct Ollama API format
        mock_resp.json.return_value = {
            "embedding": [0.1, 0.2, 0.3]
        }
        mock_post.return_value = mock_resp

        result = self.client.get_embedding("test text")
        self.assertEqual(result, [0.1, 0.2, 0.3])
        mock_post.assert_called_once()

    @patch('requests.post')
    def test_error_handling_retry_failure(self, mock_post):
        # Mock requests exception on all calls to verify it retries and then raises the exception
        mock_post.side_effect = requests.exceptions.Timeout("Connection timed out")

        with self.assertRaises(requests.exceptions.Timeout):
            self.client.generate_completion("test prompt")
        
        # Verify it retried 2 times
        self.assertEqual(mock_post.call_count, 2)

    @patch.dict('os.environ', {'OLLAMA_MODEL': 'gemma'})
    def test_model_resolution_env_alias(self):
        client = OllamaClient(config_path='non_existent_config.yaml')
        self.assertEqual(client.model_name, 'gemma4:26b')

    @patch.dict('os.environ', {'OLLAMA_MODEL': 'custom-model:latest'})
    def test_model_resolution_env_custom(self):
        client = OllamaClient(config_path='non_existent_config.yaml')
        self.assertEqual(client.model_name, 'custom-model:latest')

    @patch('requests.post')
    def test_get_embedding_caching(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "embedding": [0.1, 0.2, 0.3]
        }
        mock_post.return_value = mock_resp

        # Request same embedding twice
        r1 = self.client.get_embedding("same text")
        r2 = self.client.get_embedding("same text")

        self.assertEqual(r1, [0.1, 0.2, 0.3])
        self.assertEqual(r2, [0.1, 0.2, 0.3])
        
        # Verify requests.post was only invoked once (second request hit the cache)
        mock_post.assert_called_once()
