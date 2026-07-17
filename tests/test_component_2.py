import unittest
from unittest.mock import patch, MagicMock, mock_open
import io
import json
from component_2_so_extractor.extract_so_logs import (
    extract_dlq_logs,
    extract_unmapped_logs,
    load_es_extract_state,
    save_es_extract_state,
    _unwrap_cbor_tagged,
    _decode_dlq_segment,
)


def _decoded_dlq_line(message="dummy dlq log", reason="some rejection reason"):
    """Builds one line of logstash-dlq-decode's real JSON output shape,
    matching what was observed against a real Security Onion DLQ segment:
    the actual fields live under event -> ["java.util.HashMap", {"DATA":
    ["org.logstash.ConvertedMap", {...}]}]."""
    return json.dumps({
        "timestamp": "2026-01-01T00:00:00.000000000Z",
        "event": ["java.util.HashMap", {"DATA": ["org.logstash.ConvertedMap", {
            "message": ["org.jruby.RubyString", message],
        }]}],
        "plugin_type": "elasticsearch",
        "plugin_id": "abc123",
        "reason": reason,
    }).encode('utf-8')


def _mock_ssh_for_dlq(segment_listing: bytes, segment_contents: dict):
    """Configures exec_command to dispatch based on command text: the `ls
    .../*.log` listing command reads via `stdout.read()`; `cat <path>`
    commands stream raw bytes via `stdout.channel.recv()`, matching
    extract_dlq_logs' two distinct SSH read paths."""
    mock_ssh = MagicMock()

    def exec_command_side_effect(command):
        stdin = MagicMock()
        stderr = MagicMock()
        stderr.read.return_value = b''
        if command.endswith("2>/dev/null'"):
            stdout = MagicMock()
            stdout.read.return_value = segment_listing
        else:
            path = command.rsplit('cat ', 1)[-1].strip()
            data = segment_contents.get(path, b'')
            stdout = MagicMock()
            stdout.channel.recv.side_effect = [data, b'']
        return (stdin, stdout, stderr)

    mock_ssh.exec_command.side_effect = exec_command_side_effect
    return mock_ssh


class TestComponent2(unittest.TestCase):
    @patch('component_2_so_extractor.extract_so_logs.subprocess.run')
    @patch('paramiko.SSHClient')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_dlq_logs(self, mock_file, mock_ssh_client, mock_subprocess_run):
        mock_ssh = _mock_ssh_for_dlq(
            segment_listing=b'/nsm/logstash/dead_letter_queue/search/1.log\n',
            segment_contents={'/nsm/logstash/dead_letter_queue/search/1.log': b'raw-segment-bytes'},
        )
        mock_ssh_client.return_value = mock_ssh
        mock_subprocess_run.return_value = MagicMock(
            returncode=0, stdout=_decoded_dlq_line() + b'\n', stderr=b''
        )

        row_count = extract_dlq_logs('admin', 'tailscale_node', 'dummy_dlq.jsonl')

        mock_ssh.connect.assert_called_once_with(hostname='tailscale_node', username='admin')
        mock_ssh.exec_command.assert_any_call(
            "sh -c 'ls /nsm/logstash/dead_letter_queue/*/*.log 2>/dev/null'"
        )
        mock_ssh.exec_command.assert_any_call("cat /nsm/logstash/dead_letter_queue/search/1.log")
        mock_file.assert_called_once_with('dummy_dlq.jsonl', 'w', encoding='utf-8')
        self.assertEqual(row_count, 1)

    @patch('component_2_so_extractor.extract_so_logs.subprocess.run')
    @patch('paramiko.SSHClient')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_dlq_logs_with_sudo(self, mock_file, mock_ssh_client, mock_subprocess_run):
        mock_ssh = _mock_ssh_for_dlq(
            segment_listing=b'/nsm/logstash/dead_letter_queue/search/1.log\n',
            segment_contents={'/nsm/logstash/dead_letter_queue/search/1.log': b'raw-segment-bytes'},
        )
        mock_ssh_client.return_value = mock_ssh
        mock_subprocess_run.return_value = MagicMock(
            returncode=0, stdout=_decoded_dlq_line() + b'\n', stderr=b''
        )

        row_count = extract_dlq_logs('admin', 'tailscale_node', 'dummy_dlq.jsonl', use_sudo=True)

        mock_ssh.exec_command.assert_any_call(
            "sudo sh -c 'ls /nsm/logstash/dead_letter_queue/*/*.log 2>/dev/null'"
        )
        mock_ssh.exec_command.assert_any_call("sudo cat /nsm/logstash/dead_letter_queue/search/1.log")
        self.assertEqual(row_count, 1)

    @patch('component_2_so_extractor.extract_so_logs.subprocess.run')
    @patch('paramiko.SSHClient')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_dlq_logs_skips_undecodable_segment(self, mock_file, mock_ssh_client, mock_subprocess_run):
        """A segment that fails to decode (e.g. the one Logstash is actively
        writing) must be skipped with a warning, not abort extraction of the
        other, decodable segments."""
        mock_ssh = _mock_ssh_for_dlq(
            segment_listing=(
                b'/nsm/logstash/dead_letter_queue/search/1.log\n'
                b'/nsm/logstash/dead_letter_queue/search/2.log\n'
            ),
            segment_contents={
                '/nsm/logstash/dead_letter_queue/search/1.log': b'truncated-mid-write',
                '/nsm/logstash/dead_letter_queue/search/2.log': b'raw-segment-bytes',
            },
        )
        mock_ssh_client.return_value = mock_ssh
        mock_subprocess_run.side_effect = [
            MagicMock(returncode=1, stdout=b'', stderr=b'record 3: unexpected EOF'),
            MagicMock(returncode=0, stdout=_decoded_dlq_line() + b'\n', stderr=b''),
        ]

        row_count = extract_dlq_logs('admin', 'tailscale_node', 'dummy_dlq.jsonl')

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
                    {"_source": {"@timestamp": "2026-01-01T00:00:00Z", "message": "unmapped error"}}
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

        row_count, max_timestamp = extract_unmapped_logs('10.0.0.2', 'user', 'pass', 10, 'now-24h', 'dummy_es.jsonl')

        self.assertEqual(row_count, 1)
        self.assertEqual(max_timestamp, "2026-01-01T00:00:00Z")
        mock_get.assert_any_call(
            "https://10.0.0.2:9200/_search?scroll=2m",
            auth=unittest.mock.ANY,
            verify=False,
            headers=unittest.mock.ANY,
            json=unittest.mock.ANY
        )
        mock_delete.assert_called_once()

        # `since` is applied as an exclusive lower bound ("gt", not "gte") so a
        # cursor from a previous run's max-seen timestamp isn't re-included.
        first_call_payload = mock_get.call_args_list[0].kwargs['json']
        self.assertEqual(
            first_call_payload['query']['bool']['filter'][0]['range']['@timestamp'],
            {"gt": "now-24h", "lte": "now"}
        )

    @patch('requests.delete')
    @patch('requests.get')
    @patch('builtins.open', new_callable=mock_open)
    def test_extract_unmapped_logs_no_hits_returns_none_timestamp(self, mock_file, mock_get, mock_delete):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"_scroll_id": None, "hits": {"hits": []}}
        mock_get.side_effect = [mock_resp]

        row_count, max_timestamp = extract_unmapped_logs('10.0.0.2', 'user', 'pass', 10, 'now-24h', 'dummy_es.jsonl')

        self.assertEqual(row_count, 0)
        self.assertIsNone(max_timestamp)
        mock_delete.assert_not_called()


class TestEsExtractState(unittest.TestCase):
    def test_load_es_extract_state_missing_file_returns_none(self):
        self.assertIsNone(load_es_extract_state('/nonexistent/path/state.json'))

    @patch('builtins.open', new_callable=mock_open, read_data='not valid json')
    def test_load_es_extract_state_corrupt_file_returns_none(self, mock_file):
        self.assertIsNone(load_es_extract_state('dummy_state.json'))

    @patch('builtins.open', new_callable=mock_open, read_data='{"last_timestamp": "2026-01-01T00:00:00Z"}')
    def test_load_es_extract_state_reads_cursor(self, mock_file):
        self.assertEqual(load_es_extract_state('dummy_state.json'), "2026-01-01T00:00:00Z")

    @patch('builtins.open', new_callable=mock_open)
    def test_save_es_extract_state_writes_cursor(self, mock_file):
        save_es_extract_state('dummy_state.json', "2026-01-02T00:00:00Z")
        mock_file.assert_called_once_with('dummy_state.json', 'w', encoding='utf-8')
        written = ''.join(call.args[0] for call in mock_file().write.call_args_list)
        self.assertEqual(json.loads(written), {"last_timestamp": "2026-01-02T00:00:00Z"})


class TestDlqDecodeHelpers(unittest.TestCase):
    def test_unwrap_cbor_tagged_nested(self):
        tagged = ["org.logstash.ConvertedMap", {
            "@timestamp": ["org.logstash.Timestamp", "2026-01-01T00:00:00Z"],
            "tags": ["org.logstash.ConvertedList", [
                ["org.jruby.RubyString", "a"],
                ["org.jruby.RubyString", "b"],
            ]],
            "missing": ["org.jruby.RubyNil", None],
            "count": 3,
            "ok": False,
        }]
        result = _unwrap_cbor_tagged(tagged)
        self.assertEqual(result, {
            "@timestamp": "2026-01-01T00:00:00Z",
            "tags": ["a", "b"],
            "missing": None,
            "count": 3,
            "ok": False,
        })

    @patch('component_2_so_extractor.extract_so_logs.subprocess.run')
    def test_decode_dlq_segment_yields_message_and_reason(self, mock_subprocess_run):
        mock_subprocess_run.return_value = MagicMock(
            returncode=0, stdout=_decoded_dlq_line("hello", "index rejected") + b'\n', stderr=b''
        )
        results = list(_decode_dlq_segment(b'irrelevant-input-bytes'))
        self.assertEqual(results, [("hello", "index rejected")])

    @patch('component_2_so_extractor.extract_so_logs.subprocess.run')
    def test_decode_dlq_segment_raises_on_nonzero_exit(self, mock_subprocess_run):
        mock_subprocess_run.return_value = MagicMock(
            returncode=1, stdout=b'', stderr=b'record 3: unexpected EOF'
        )
        with self.assertRaises(RuntimeError):
            list(_decode_dlq_segment(b'truncated'))


if __name__ == '__main__':
    unittest.main()
