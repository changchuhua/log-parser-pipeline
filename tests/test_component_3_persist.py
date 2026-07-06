import unittest
import os
import json
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from component_3_unified_parser.main_parser import run_logparser_llm

class TestComponent3Persist(unittest.TestCase):
    def setUp(self):
        # Create a temporary directory for cache testing
        self.test_dir = tempfile.mkdtemp()
        self.cache_dir = os.path.join(self.test_dir, 'cache')
        self.input_dir = os.path.join(self.test_dir, 'input')
        self.output_dir = os.path.join(self.test_dir, 'output')
        os.makedirs(self.input_dir)
        os.makedirs(self.output_dir)

        # Create dummy input ECS loghub file
        self.input_file = os.path.join(self.input_dir, 'loghub_ecs.jsonl')
        with open(self.input_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                "@timestamp": "2026-01-01 12:00:00",
                "message": "User admin logged in",
                "event": {"id": "1"}
            }) + '\n')

    def tearDown(self):
        # Cleanup temp directory
        shutil.rmtree(self.test_dir)

    @patch('core.logparser_llm.llm_extractor.OllamaClient')
    def test_run_logparser_llm_persistence_and_timer(self, mock_client_class):
        # Setup Ollama client mock to return static completion and embedding
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = "User <*> logged in"
        mock_client.get_embedding.return_value = [0.1, 0.2, 0.3]
        mock_client_class.return_value = mock_client

        # 1. Run parser with persist enabled (Cold run - cache empty)
        run_logparser_llm(
            input_files=[self.input_file],
            output_dir=self.output_dir,
            persist=True,
            cache_dir=self.cache_dir
        )

        # Verify cache file was written to the cache directory
        cache_file = os.path.join(self.cache_dir, 'logparser_llm_cache.json')
        self.assertTrue(os.path.exists(cache_file))

        with open(cache_file, 'r', encoding='utf-8') as f:
            cache_data = json.load(f)
        self.assertIn("User <*> logged in", cache_data)

        # Verify profile timer file was written to output directory
        profile_file = os.path.join(self.output_dir, 'parsed_loghub_ecs_profile.json')
        self.assertTrue(os.path.exists(profile_file))

        with open(profile_file, 'r', encoding='utf-8') as pf:
            profile_data = json.load(pf)
        self.assertIn("time_taken_seconds", profile_data)
        self.assertIsInstance(profile_data["time_taken_seconds"], float)
        self.assertGreaterEqual(profile_data["time_taken_seconds"], 0.0)

        # 2. Run again with persist enabled (Warm run - should load from cache)
        # Mock generate_completion to raise error if called (verifying it does not use the LLM)
        mock_client.generate_completion.side_effect = AssertionError("Should have used strict cache match!")

        # Modify the log input file to have another line that should match strictly
        with open(self.input_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({
                "@timestamp": "2026-01-01 12:00:01",
                "message": "User guest logged in",
                "event": {"id": "2"}
            }) + '\n')

        # Run parser again
        try:
            run_logparser_llm(
                input_files=[self.input_file],
                output_dir=self.output_dir,
                persist=True,
                cache_dir=self.cache_dir
            )
        except AssertionError as e:
            self.fail(f"Parser called LLM extractor instead of prefix tree cache: {e}")

        # Check output contains the loaded parsed template
        output_file = os.path.join(self.output_dir, 'parsed_loghub_ecs.jsonl')
        with open(output_file, 'r', encoding='utf-8') as f:
            output_data = json.loads(f.readline().strip())
        self.assertEqual(output_data['parsed_template'], "User <*> logged in")
