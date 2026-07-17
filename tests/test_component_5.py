import json
import os
import tempfile
import unittest
from unittest.mock import patch

from component_5_deployer.core.compiler import IngestPipelineCompiler
from component_5_deployer.core.es_client import ElasticsearchDeployer
from component_5_deployer.core.validator import IngestPipelineValidator
from component_5_deployer.core.salt_sftp import SaltstackDeployer


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


if __name__ == '__main__':
    unittest.main()
