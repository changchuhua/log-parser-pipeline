import os
import tempfile
import unittest
import yaml
from unittest.mock import MagicMock, patch

from component_3_unified_parser.core.librelog.parser import LibreLogParser
import component_3_unified_parser.core.librelog.parser as librelog_parser_module

# LibreLogParser is imported here via the component_3_unified_parser.* dotted
# path, so its relative imports (DrainParser, etc.) resolve into THIS module
# object -- @patch.object(librelog_parser_module, 'X') must target the same
# one, not the short `core.librelog.parser` alias (a separate module instance
# under this repo's dual-pythonpath test setup; see other test files' notes).


def _write_config(memory_mode=None):
    fd, path = tempfile.mkstemp(suffix='.yaml')
    cfg = {'librelog': {}}
    if memory_mode is not None:
        cfg['librelog']['memory_mode'] = memory_mode
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        yaml.safe_dump(cfg, f)
    return path


class TestMemoryModeWiring(unittest.TestCase):
    @patch('component_3_unified_parser.core.librelog.parser.OllamaClient')
    def test_defaults_to_production(self, mock_client_class):
        path = _write_config()
        self.addCleanup(os.remove, path)
        parser = LibreLogParser(dataset_name='default', config_path=path)
        self.assertEqual(parser.memory_mode, 'production')

    @patch('component_3_unified_parser.core.librelog.parser.OllamaClient')
    def test_original_mode_reaches_init(self, mock_client_class):
        path = _write_config(memory_mode='original')
        self.addCleanup(os.remove, path)
        parser = LibreLogParser(dataset_name='default', config_path=path)
        self.assertEqual(parser.memory_mode, 'original')


class TestMemoryModeBehavior(unittest.TestCase):
    """The paper's Template Memory mechanism (RegexTemplateManager, inside
    LlamaParser.parse()) already runs unconditionally either way -- these tests
    cover the one real, disclosed deviation: production's cache_map exact-string
    pre-filter (persisted via DummyMemory/--use-cache), which "original" mode
    disables entirely per config.yaml's librelog.memory_mode."""

    def _make_parser(self, memory_mode):
        path = _write_config(memory_mode=memory_mode)
        self.addCleanup(os.remove, path)
        with patch('component_3_unified_parser.core.librelog.parser.OllamaClient'):
            parser = LibreLogParser(dataset_name='default', config_path=path)
        parser.llama_parser = MagicMock()
        return parser

    @patch.object(librelog_parser_module, 'DrainParser')
    def test_production_cache_hit_skips_llm(self, mock_drain_cls):
        parser = self._make_parser('production')
        parser.memory.memory = [{
            'raw_log': 'user bob logged in',
            'template': 'user <*> logged in',
            'group_key': (4, ('user',)),
        }]
        mock_drain_cls.return_value.parse.return_value = [
            ['user bob logged in', 'E1', 'irrelevant'],
        ]

        results = parser.parse([{'id': '1', 'message': 'user bob logged in'}])

        parser.llama_parser.parse.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['template'], 'user <*> logged in')

    @patch.object(librelog_parser_module, 'DrainParser')
    def test_original_mode_ignores_preseeded_cache_and_calls_llm(self, mock_drain_cls):
        parser = self._make_parser('original')
        # Same pre-seeded entry as the production test -- in "original" mode
        # this must NOT produce a cache hit, since cache_map is disabled.
        parser.memory.memory = [{
            'raw_log': 'user bob logged in',
            'template': 'user <*> logged in',
            'group_key': (4, ('user',)),
        }]
        mock_drain_cls.return_value.parse.return_value = [
            ['user bob logged in', 'E1', 'irrelevant'],
        ]
        parser.llama_parser.parse.return_value = [
            ('user bob logged in', 'E1', 'user <*> logged in'),
        ]

        results = parser.parse([{'id': '1', 'message': 'user bob logged in'}])

        parser.llama_parser.parse.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['template'], 'user <*> logged in')

    @patch.object(librelog_parser_module, 'DrainParser')
    def test_production_reuses_freshly_generated_template_within_same_run(self, mock_drain_cls):
        parser = self._make_parser('production')
        # Two separate clusters with identical raw content (simulating a case
        # where grouping didn't already merge them) -- production should only
        # need one LLM call, reusing cache_map for the second occurrence.
        mock_drain_cls.return_value.parse.return_value = [
            ['foo bar', 'E1', 'irrelevant'],
            ['foo bar', 'E2', 'irrelevant'],
        ]
        parser.llama_parser.parse.return_value = [('foo bar', 'E1', 'foo <*>')]

        parser.parse([
            {'id': '1', 'message': 'foo bar'},
            {'id': '2', 'message': 'foo bar'},
        ])

        self.assertEqual(parser.llama_parser.parse.call_count, 1)
        self.assertEqual(len(parser.memory.memory), 1)

    @patch.object(librelog_parser_module, 'DrainParser')
    def test_original_mode_calls_llm_for_every_occurrence_and_never_persists(self, mock_drain_cls):
        parser = self._make_parser('original')
        mock_drain_cls.return_value.parse.return_value = [
            ['foo bar', 'E1', 'irrelevant'],
            ['foo bar', 'E2', 'irrelevant'],
        ]
        parser.llama_parser.parse.return_value = [('foo bar', 'E1', 'foo <*>')]

        parser.parse([
            {'id': '1', 'message': 'foo bar'},
            {'id': '2', 'message': 'foo bar'},
        ])

        self.assertEqual(parser.llama_parser.parse.call_count, 2)
        self.assertEqual(len(parser.memory.memory), 0)


if __name__ == '__main__':
    unittest.main()
