import unittest
from unittest.mock import patch, MagicMock, mock_open
import io
import json
from component_2_so_extractor.extract_so_logs import extract_dlq_logs, extract_unmapped_logs

class TestComponent2(unittest.TestCase):
    @patch('paramiko.SSHClient')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_dlq_logs(self, mock_file, mock_ssh_client):
        mock_ssh = MagicMock()
        mock_ssh_client.return_value = mock_ssh
        
        mock_stdout = io.StringIO('{"message": "dummy dlq log"}\n')
        mock_stderr = io.BytesIO(b'')
        mock_ssh.exec_command.return_value = (None, mock_stdout, mock_stderr)
        
        row_count = extract_dlq_logs('admin', 'tailscale_node', 'dummy_dlq.jsonl')
        
        mock_ssh.connect.assert_called_once_with(hostname='tailscale_node', username='admin')
        mock_ssh.exec_command.assert_called_once_with("sudo cat /nsm/logstash/dead_letter_queue/main/* /nsm/logstash/dead_letter_queue/search/*")
        mock_file.assert_called_once_with('dummy_dlq.jsonl', 'w', encoding='utf-8')
        self.assertEqual(row_count, 1)

    @patch('requests.delete')
    @patch('requests.get')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_unmapped_logs(self, mock_file, mock_get, mock_delete):
        mock_resp1 = MagicMock()
        mock_resp1.json.return_value = {
            "_scroll_id": "dummy_id",
            "hits": {
                "hits": [
                    {"_source": {"@timestamp": "2026-01-01", "message": "unmapped error"}}
                ]
            }
        }
        
        mock_resp2 = MagicMock()
        mock_resp2.json.return_value = {
            "_scroll_id": "dummy_id",
            "hits": {
                "hits": []
            }
        }
        
        mock_get.side_effect = [mock_resp1, mock_resp2]
        
        row_count = extract_unmapped_logs('10.0.0.2', 'user', 'pass', 10, 'now-24h', 'dummy_es.jsonl')
        
        self.assertEqual(row_count, 1)
        mock_get.assert_any_call(
            "https://10.0.0.2:9200/_search?scroll=2m",
            auth=unittest.mock.ANY,
            verify=False,
            headers=unittest.mock.ANY,
            json=unittest.mock.ANY
        )
        mock_delete.assert_called_once()
