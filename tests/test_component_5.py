import json
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from component_5_deployer.core.compiler import IngestPipelineCompiler
from component_5_deployer.core.es_client import ElasticsearchDeployer
from component_5_deployer.core.validator import IngestPipelineValidator
from component_5_deployer.core.salt_sftp import SaltstackDeployer
from component_5_deployer.core.global_custom_wirer import build_wired_pipeline, GLOBAL_CUSTOM_PIPELINE_NAME


DEPLOYER_CONFIG = {
    "dry_run": True,
    "pipeline_name": "so_custom_ingest_pipeline",
    "parsed_logs_path": "",
    "elasticsearch": {"port": 9200, "verify_certs": False},
    "saltstack": {
        "tmp_dir": "/tmp/",
        "destination_dir": "/opt/so/saltstack/local/salt/elasticsearch/files/ingest/",
        "file_owner": "so-elasticsearch:so-elasticsearch",
    },
}


class TestDeployerConfigShape(unittest.TestCase):
    """Regression coverage for a real bug: main_deployer.py used to pass the
    *entire* top-level config (where elasticsearch/saltstack live nested
    under a "deployer" key) to these constructors, but they all expect the
    deployer-scoped sub-dict directly — every real run hit a KeyError on
    'elasticsearch' immediately, before ever reaching Elasticsearch or SSH.
    These tests pin the config shape these classes actually require."""

    @patch.dict('os.environ', {'SO_IP': '10.0.0.1', 'SO_USER': 'user', 'SO_PASS': 'pass'})
    def test_elasticsearch_deployer_accepts_deployer_scoped_config(self):
        deployer = ElasticsearchDeployer(DEPLOYER_CONFIG)
        self.assertEqual(deployer.url, "https://10.0.0.1:9200")
        self.assertFalse(deployer.verify)

    @patch.dict('os.environ', {'SO_IP': '10.0.0.1', 'SO_USER': 'user', 'SO_PASS': 'pass'})
    def test_validator_accepts_deployer_scoped_config(self):
        validator = IngestPipelineValidator(DEPLOYER_CONFIG)
        self.assertEqual(validator.url, "https://10.0.0.1:9200")

    @patch.dict('os.environ', {'TAILSCALE_NODE': 'so-host'})
    def test_salt_deployer_accepts_deployer_scoped_config(self):
        deployer = SaltstackDeployer(DEPLOYER_CONFIG)
        self.assertEqual(deployer.tmp_dir, "/tmp/")
        self.assertEqual(deployer.dest_dir, "/opt/so/saltstack/local/salt/elasticsearch/files/ingest/")
        self.assertEqual(deployer.file_owner, "so-elasticsearch:so-elasticsearch")

    @patch.dict('os.environ', {'SO_IP': '10.0.0.1', 'SO_USER': 'u', 'SO_PASS': 'p'})
    def test_constructors_reject_full_top_level_config(self):
        """The bug this guards against: passing the *full* config (as
        config.yaml actually shapes it, with elasticsearch/saltstack nested
        under "deployer") must fail loudly with a KeyError, confirming these
        classes really do require the unwrapped deployer-scoped dict rather
        than silently reading the wrong/default values."""
        full_config = {"deployer": DEPLOYER_CONFIG}
        with self.assertRaises(KeyError):
            ElasticsearchDeployer(full_config)


class TestIngestPipelineCompiler(unittest.TestCase):
    def test_compile_template_to_grok_maps_tags(self):
        compiler = IngestPipelineCompiler()
        grok = compiler.compile_template_to_grok(
            "[client <LOI>] script not found or unable to stat: <OBN>"
        )
        self.assertEqual(
            grok,
            r"\[client %{IP:source.ip}\] script not found or unable to stat: %{NOTSPACE:process.name}"
        )

    def test_build_pipeline_json_from_real_template(self):
        """Real template/message pulled from an actual loghub logparser-llm
        run, matching what was validated live against Elasticsearch's
        /_simulate endpoint during Component 5 dry-run testing."""
        compiler = IngestPipelineCompiler()
        record = {
            "message": "[client 202.107.54.186] script not found or unable to stat: /var/www/cgi-bin/awstats.pl",
            "parsed_template": "[client <LOI>] script not found or unable to stat: <OBN>",
        }
        with tempfile.NamedTemporaryFile(mode='w', suffix='.jsonl', delete=False) as f:
            f.write(json.dumps(record) + "\n")
            path = f.name
        try:
            pipeline_json = compiler.build_pipeline_json(path, "test_pipeline")
            grok_processor = pipeline_json["processors"][0]["grok"]
            self.assertEqual(grok_processor["field"], "message")
            self.assertEqual(len(grok_processor["patterns"]), 1)
            self.assertIn("%{IP:source.ip}", grok_processor["patterns"][0])
        finally:
            os.unlink(path)


class TestGlobalCustomWirer(unittest.TestCase):
    """No network/mocking needed -- build_wired_pipeline is a pure function.
    Written with extra care since the live Security Onion box was down during
    this feature's development, so these unit tests are the only verification
    this logic has had; it has not been exercised against a real cluster."""

    def test_appends_processor_when_not_present(self):
        current = {"processors": [{"set": {"field": "event.module", "value": "elastic_agent"}}]}
        merged, changed = build_wired_pipeline(current, "so_custom_ingest_pipeline", "ctx.event?.category == null")
        self.assertTrue(changed)
        self.assertEqual(len(merged["processors"]), 2)
        self.assertEqual(merged["processors"][0], current["processors"][0])
        new_proc = merged["processors"][1]["pipeline"]
        self.assertEqual(new_proc["name"], "so_custom_ingest_pipeline")
        self.assertEqual(new_proc["if"], "ctx.event?.category == null")
        self.assertTrue(new_proc["ignore_missing_pipeline"])

    def test_noop_when_already_wired(self):
        current = {
            "processors": [
                {"set": {"field": "event.module", "value": "elastic_agent"}},
                {"pipeline": {"name": "so_custom_ingest_pipeline", "if": "ctx.event?.category == null"}},
            ]
        }
        merged, changed = build_wired_pipeline(current, "so_custom_ingest_pipeline", "ctx.event?.category == null")
        self.assertFalse(changed)
        # unchanged means unchanged -- same object contents, no processor added
        self.assertEqual(merged, current)

    def test_does_not_mutate_original_dict(self):
        current = {"processors": [{"set": {"field": "a", "value": "b"}}]}
        original_processors = list(current["processors"])
        build_wired_pipeline(current, "so_custom_ingest_pipeline", "ctx.event?.category == null")
        self.assertEqual(current["processors"], original_processors)

    def test_strips_version_field_from_merged_output(self):
        current = {"version": 3, "processors": []}
        merged, changed = build_wired_pipeline(current, "so_custom_ingest_pipeline", "ctx.event?.category == null")
        self.assertTrue(changed)
        self.assertNotIn("version", merged)

    def test_handles_missing_processors_key(self):
        current = {}
        merged, changed = build_wired_pipeline(current, "so_custom_ingest_pipeline", "ctx.event?.category == null")
        self.assertTrue(changed)
        self.assertEqual(len(merged["processors"]), 1)

    def test_pipeline_name_constant(self):
        self.assertEqual(GLOBAL_CUSTOM_PIPELINE_NAME, "global@custom")


class TestSaltstackDeployerExactFilename(unittest.TestCase):
    """deploy_persistently() must keep appending .json (existing sudoers
    grants depend on that wildcard match); deploy_persistently_exact() must
    use the filename as-is, since Security-Onion-native pipeline files like
    "global@custom" have no extension and so-elasticsearch-pipelines uses
    the filename itself as the target pipeline name."""

    def _mock_ssh(self):
        mock_ssh = MagicMock()
        mock_sftp = MagicMock()
        mock_ssh.open_sftp.return_value = mock_sftp
        mock_stdout = MagicMock()
        mock_stdout.channel.recv_exit_status.return_value = 0
        mock_stderr = MagicMock()
        mock_ssh.exec_command.return_value = (MagicMock(), mock_stdout, mock_stderr)
        return mock_ssh, mock_sftp

    @patch.dict('os.environ', {'TAILSCALE_NODE': 'so-host'})
    @patch('paramiko.SSHClient')
    def test_deploy_persistently_appends_json_suffix(self, mock_ssh_client):
        mock_ssh, mock_sftp = self._mock_ssh()
        mock_ssh_client.return_value = mock_ssh

        deployer = SaltstackDeployer(DEPLOYER_CONFIG)
        deployer.deploy_persistently("so_custom_ingest_pipeline", "/tmp/local_compiled.json")

        put_args = mock_sftp.put.call_args[0]
        self.assertEqual(put_args[1], "/tmp/so_custom_ingest_pipeline.json")

        cmd = mock_ssh.exec_command.call_args[0][0]
        self.assertIn("/tmp/so_custom_ingest_pipeline.json", cmd)
        self.assertIn(
            "/opt/so/saltstack/local/salt/elasticsearch/files/ingest/so_custom_ingest_pipeline.json", cmd
        )

    @patch.dict('os.environ', {'TAILSCALE_NODE': 'so-host'})
    @patch('paramiko.SSHClient')
    def test_deploy_persistently_exact_uses_filename_verbatim(self, mock_ssh_client):
        mock_ssh, mock_sftp = self._mock_ssh()
        mock_ssh_client.return_value = mock_ssh

        deployer = SaltstackDeployer(DEPLOYER_CONFIG)
        deployer.deploy_persistently_exact("global@custom", "/tmp/global_custom_merged.json")

        put_args = mock_sftp.put.call_args[0]
        self.assertEqual(put_args[1], "/tmp/global@custom")
        self.assertNotIn(".json", put_args[1])

        cmd = mock_ssh.exec_command.call_args[0][0]
        self.assertIn("/tmp/global@custom", cmd)
        self.assertIn("/opt/so/saltstack/local/salt/elasticsearch/files/ingest/global@custom", cmd)
        self.assertNotIn("global@custom.json", cmd)

    @patch.dict('os.environ', {'TAILSCALE_NODE': 'so-host'})
    @patch('paramiko.SSHClient')
    def test_deploy_persistently_exact_raises_on_nonzero_exit(self, mock_ssh_client):
        mock_ssh, mock_sftp = self._mock_ssh()
        mock_ssh.exec_command.return_value[1].channel.recv_exit_status.return_value = 1
        mock_ssh.exec_command.return_value[2].read.return_value = b"a password is required"
        mock_ssh_client.return_value = mock_ssh

        deployer = SaltstackDeployer(DEPLOYER_CONFIG)
        with self.assertRaises(RuntimeError):
            deployer.deploy_persistently_exact("global@custom", "/tmp/global_custom_merged.json")


if __name__ == '__main__':
    unittest.main()
