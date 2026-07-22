import os
import tempfile
import unittest
import yaml
from unittest.mock import MagicMock, patch

from component_3_unified_parser.core.logbatcher.original_cache import OriginalParsingCache
from component_3_unified_parser.core.logbatcher.parser import LogBatcher
from component_3_unified_parser.core.logbatcher.parsing_cache import ParsingCache
import component_3_unified_parser.core.logbatcher.parser as logbatcher_parser_module

# @patch('core.logbatcher.parser.X') resolves to a *separate* duplicate
# module instance in this repo's dual-pythonpath test setup -- it silently
# never touches the LogBatcher class actually under test here.
# @patch.object(logbatcher_parser_module, 'X') targets the real module.


class TestOriginalParsingCache(unittest.TestCase):
    def test_hash_fast_path_on_repeat(self):
        cache = OriginalParsingCache()
        cache.add_templates('User <*> logged in from <*>', insert=True, refer_log='User bob logged in from 10.0.0.1')

        # First occurrence of this exact log necessarily goes through the tree
        # walk (insert() only pre-populates hashing_cache keyed by the
        # template's standardized form, not the log's -- a quirk faithfully
        # carried over from upstream).
        first = cache.match_event('User bob logged in from 10.0.0.1')
        self.assertEqual(first[0], 'User <*> logged in from <*>')
        self.assertEqual(cache.hit_num, 0)

        # Second identical occurrence hits the hash cache populated by the
        # first call's tree-match branch.
        second = cache.match_event('User bob logged in from 10.0.0.1')
        self.assertEqual(second[0], 'User <*> logged in from <*>')
        self.assertEqual(cache.hit_num, 1)

    def test_tree_match_on_new_instance_of_template(self):
        cache = OriginalParsingCache()
        cache.add_templates('User <*> logged in from <*>', insert=True, refer_log='User bob logged in from 10.0.0.1')

        result = cache.match_event('User alice logged in from 10.0.0.2')
        self.assertEqual(result[0], 'User <*> logged in from <*>')

    def test_no_match_on_novel_log(self):
        cache = OriginalParsingCache()
        cache.add_templates('User <*> logged in from <*>', insert=True, refer_log='User bob logged in from 10.0.0.1')

        result = cache.match_event('Disk usage at 95 percent on /dev/sda1')
        self.assertEqual(result[0], 'NoMatch')

    def test_add_templates_insert_only_when_relevant_templates_empty(self):
        """Matches parser.py's real call pattern (insert=False, relevant_templates
        left at its default []) -- confirms the LCS-merge branch is unreachable
        there, exactly mirroring upstream's own dead-code call site in
        parsing_base.py."""
        cache = OriginalParsingCache()
        cache.add_templates('User <*> logged in', insert=False, refer_log='User bob logged in')
        cache.add_templates('User <*> logged out', insert=False, refer_log='User bob logged out')

        # Both inserted as distinct templates -- no merge occurred despite
        # being LCS-similar, because relevant_templates was never populated.
        self.assertEqual(len(cache.template_list), 2)
        self.assertIn('User <*> logged in', cache.template_list)
        self.assertIn('User <*> logged out', cache.template_list)

    def test_add_templates_merge_branch_reachable_directly(self):
        """The merge capability itself still works when relevant_templates is
        explicitly supplied -- confirms it's dead at the wired call site, not
        broken as ported code. LCS similarity here is 0.833 (6 of 7 tokens
        shared), clearing the >0.8 merge threshold."""
        cache = OriginalParsingCache()
        cache.add_templates('User bob logged in successfully today', insert=True, refer_log='User bob logged in successfully today')

        cache.add_templates(
            'User alice logged in successfully today', insert=False,
            relevant_templates=['User bob logged in successfully today'], refer_log='User alice logged in successfully today'
        )
        self.assertEqual(len(cache.template_list), 1)
        self.assertEqual(cache.template_list[0], 'User <*> logged in successfully today')


class TestLogBatcherCacheModeSelection(unittest.TestCase):
    def _write_config(self, cache_mode):
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'cache_mode': cache_mode}}, f)
        self.addCleanup(os.remove, path)
        return path

    def test_default_cache_mode_is_production(self):
        parser = LogBatcher('/app/config.yaml')  # missing in this env -> config = {} -> default
        self.assertEqual(parser.cache_mode, 'production')
        self.assertIsInstance(parser.cache, ParsingCache)

    def test_explicit_production_cache_mode(self):
        path = self._write_config('production')
        parser = LogBatcher(path)
        self.assertEqual(parser.cache_mode, 'production')
        self.assertIsInstance(parser.cache, ParsingCache)

    def test_explicit_original_cache_mode(self):
        path = self._write_config('original')
        parser = LogBatcher(path)
        self.assertEqual(parser.cache_mode, 'original')
        self.assertIsInstance(parser.cache, OriginalParsingCache)


class TestNoiseHandlingUnderOriginalCacheMode(unittest.TestCase):
    """Regression guard: _handle_noise_logs()'s Tier 1 cache-match used to call
    match_log(self.cache, msg), which accesses cache.cache -- a ParsingCache-only
    API that OriginalParsingCache doesn't have. This crashed with AttributeError
    the moment any noise (DBSCAN label -1) log appeared under cache_mode:
    "original". Fixed by branching Tier 1 on cache_mode to use match_event()
    instead when appropriate."""

    @patch.object(logbatcher_parser_module, 'OllamaClient')
    @patch.object(logbatcher_parser_module, 'get_sampler')
    @patch.object(logbatcher_parser_module, 'ParsingBase')
    def test_noise_handling_does_not_crash_under_original_cache_mode(self, mock_parsing_base_class, mock_get_sampler, mock_ollama_client_class):
        mock_ollama_client_class.return_value = MagicMock()
        mock_get_sampler.return_value = MagicMock()
        mock_parsing_base_class.return_value = MagicMock()

        parser = LogBatcher('/app/config.yaml')
        parser.cache_mode = 'original'
        parser.cache = OriginalParsingCache()
        parser.noise_max_retries = 0  # skip re-queue, go straight to Tier 3 regex pre-mask

        parsed_results = {}
        counters = {'log_volume': 0, 'llm_invocations': 0, 'cache_hits': 0}
        quarantine_file = 'data/parsed/quarantine.jsonl'
        if os.path.exists(quarantine_file):
            os.remove(quarantine_file)
        self.addCleanup(lambda: os.remove(quarantine_file) if os.path.exists(quarantine_file) else None)

        noise_logs = [{"id": "n1", "message": "some unmatched noise log 12345"}]
        # Must not raise -- this is exactly the call site that used to crash.
        parser._handle_noise_logs(noise_logs, parsed_results, quarantine_file, counters)

        self.assertIn('n1', parsed_results)
        self.assertEqual(counters['log_volume'], 1)


if __name__ == '__main__':
    unittest.main()
