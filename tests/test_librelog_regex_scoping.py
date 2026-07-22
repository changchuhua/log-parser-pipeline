import os
import tempfile
import unittest
from unittest.mock import patch

import yaml

from component_3_unified_parser.core.librelog.parser import (
    LibreLogParser,
    DATASET_REGEXES,
    GLOBAL_VARIABLE_RULES,
)


class TestGlobalVariableRulesScoping(unittest.TestCase):
    """GLOBAL_VARIABLE_RULES exists to give unlisted/custom datasets (e.g.
    botsv3) some preprocessing since they have no dedicated regex list --
    it must NOT apply to any of the paper's 16 tuned datasets, since
    upstream's own call site only ever passes that dataset's own regex list.
    Regression test for a real fidelity bug: this was previously combined
    unconditionally for every dataset."""

    def _make_parser(self, dataset_name):
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            yaml.safe_dump({}, f)
        self.addCleanup(os.remove, path)
        with patch('component_3_unified_parser.core.librelog.parser.OllamaClient'):
            return LibreLogParser(dataset_name=dataset_name, config_path=path)

    def test_official_dataset_uses_only_its_own_regex_list(self):
        parser = self._make_parser('HDFS')
        self.assertEqual(parser.rex, DATASET_REGEXES['HDFS'])
        for rule in GLOBAL_VARIABLE_RULES:
            self.assertNotIn(rule, parser.rex)

    def test_every_official_dataset_excludes_global_rules(self):
        for name in DATASET_REGEXES:
            parser = self._make_parser(name)
            self.assertEqual(parser.rex, DATASET_REGEXES[name])

    def test_unlisted_custom_dataset_gets_global_rules(self):
        parser = self._make_parser('botsv3')
        self.assertEqual(parser.rex, GLOBAL_VARIABLE_RULES)


if __name__ == '__main__':
    unittest.main()
