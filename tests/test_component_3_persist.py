import unittest
import os
import json
import tempfile
import shutil
from unittest.mock import patch, MagicMock
from component_3_unified_parser.main_parser import run_logparser_llm

# Explicit imports to ensure core submodules are loaded before unittest.mock.patch decorators are evaluated
import core.librelog.parser
import core.logbatcher.parser
import core.logparser_llm.llm_extractor

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
        
        # Add get_usage mock behavior representing the real client tracking properties
        mock_client.get_usage.return_value = {
            "invocations": 1,
            "prompt_tokens": 15,
            "completion_tokens": 5,
            "total_tokens": 20
        }
        mock_client_class.return_value = mock_client

        # 1. Run parser with use_cache and write_cache enabled (Cold run - cache empty)
        run_logparser_llm(
            input_files=[self.input_file],
            output_dir=self.output_dir,
            use_cache=True,
            write_cache=True,
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

        # Verify token usage tracking metrics are inside the profile file
        self.assertIn("llm_invocations", profile_data)
        self.assertIn("total_tokens", profile_data)
        self.assertEqual(profile_data["llm_invocations"], 1)
        self.assertEqual(profile_data["total_tokens"], 20)

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
                use_cache=True,
                write_cache=True,
                cache_dir=self.cache_dir
            )
        except AssertionError as e:
            self.fail(f"Parser called LLM extractor instead of prefix tree cache: {e}")

        # Check output contains the loaded parsed template
        output_file = os.path.join(self.output_dir, 'parsed_loghub_ecs.jsonl')
        with open(output_file, 'r', encoding='utf-8') as f:
            output_data = json.loads(f.readline().strip())
        self.assertEqual(output_data['parsed_template'], "User <*> logged in")

    @patch('core.logparser_llm.llm_extractor.OllamaClient')
    def test_run_logparser_llm_time_limit(self, mock_client_class):
        # Setup Ollama client mock
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = "User <*> logged in"
        mock_client.get_embedding.return_value = [0.1, 0.2, 0.3]
        mock_client.get_usage.return_value = {
            "invocations": 1,
            "prompt_tokens": 15,
            "completion_tokens": 5,
            "total_tokens": 20
        }
        mock_client_class.return_value = mock_client

        # Create input file with multiple logs
        with open(self.input_file, 'w', encoding='utf-8') as f:
            for i in range(10):
                f.write(json.dumps({
                    "@timestamp": f"2026-01-01 12:00:{i:02d}",
                    "message": f"User admin_{i} logged in",
                    "event": {"id": str(i)}
                }) + '\n')

        # Run parser with a negative time limit to force instant timeout
        run_logparser_llm(
            input_files=[self.input_file],
            output_dir=self.output_dir,
            use_cache=False,
            write_cache=False,
            time_limit=-1.0
        )

        # Output should be written but contain 0 parsed lines (due to instant timeout)
        output_file = os.path.join(self.output_dir, 'parsed_loghub_ecs.jsonl')
        self.assertTrue(os.path.exists(output_file))
        with open(output_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 0)

    @patch('core.logbatcher.parser.OllamaClient')
    def test_logbatcher_time_limit(self, mock_client_class):
        # Setup Ollama client mock
        mock_client = MagicMock()
        mock_client.get_embedding.return_value = [0.1, 0.2, 0.3]
        mock_client_class.return_value = mock_client

        from core.logbatcher.parser import LogBatcher
        parser = LogBatcher()
        
        logs = [{"id": str(i), "message": f"Log message {i}"} for i in range(10)]
        
        # Run with negative time limit to trigger instant timeout
        results = parser.parse(logs, time_limit=-1.0)
        self.assertEqual(len(results), 0)

    @patch('core.librelog.parser.OllamaClient')
    def test_librelog_time_limit(self, mock_client_class):
        # Setup Ollama client mock
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = "Log message <*>"
        mock_client_class.return_value = mock_client

        from core.librelog.parser import LibreLogParser
        parser = LibreLogParser()
        
        logs = [{"id": str(i), "message": f"Log message {i}"} for i in range(10)]
        
        # Run with negative time limit to trigger instant timeout
        results = parser.parse(logs, time_limit=-1.0)
        self.assertEqual(len(results), 0)

    @patch('core.logparser_llm.llm_extractor.OllamaClient')
    def test_run_logparser_llm_granular_cache_toggles(self, mock_client_class):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = "User <*> logged in"
        mock_client.get_embedding.return_value = [0.1, 0.2, 0.3]
        mock_client.get_usage.return_value = {
            "invocations": 1,
            "prompt_tokens": 15,
            "completion_tokens": 5,
            "total_tokens": 20
        }
        mock_client_class.return_value = mock_client

        # Create pre-existing cache file manually
        os.makedirs(self.cache_dir, exist_ok=True)
        cache_file = os.path.join(self.cache_dir, 'logparser_llm_cache.json')
        with open(cache_file, 'w', encoding='utf-8') as cf:
            json.dump(["User <*> logged in"], cf)

        # 1. Run with use_cache=True, write_cache=False (should load but not write back)
        # Mock generate_completion to raise AssertionError if called
        mock_client.generate_completion.side_effect = AssertionError("Should have loaded template from cache!")

        run_logparser_llm(
            input_files=[self.input_file],
            output_dir=self.output_dir,
            use_cache=True,
            write_cache=False,
            cache_dir=self.cache_dir
        )

        # Now remove the cache file and run with use_cache=False, write_cache=True (should NOT load, should write)
        os.remove(cache_file)
        mock_client.generate_completion.side_effect = None  # Reset side effect
        
        run_logparser_llm(
            input_files=[self.input_file],
            output_dir=self.output_dir,
            use_cache=False,
            write_cache=True,
            cache_dir=self.cache_dir
        )
        
        # Verify it wrote to cache
        self.assertTrue(os.path.exists(cache_file))


