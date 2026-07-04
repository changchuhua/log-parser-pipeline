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
