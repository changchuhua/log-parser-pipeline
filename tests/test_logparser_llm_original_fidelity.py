"""Regression guards for the three LogParser-LLM config-toggleable "original" modes
added to close gaps found in the code-vs-paper comparison (arXiv:2408.13727):

- prompt_mode: paper's plain-text "Parsed Log: ..." substitution vs our JSON output.
- merge_mode: paper's LLM-driven check+verify merge vs our structural substitution.
- match_llm_mode: paper's "loose match still queries the LLM" vs our "loose match is
  a full cache hit" behavior.

All three default to "production" (existing behavior unchanged); "original" is the
opt-in faithful port.
"""
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import yaml

from component_3_unified_parser.core.logparser_llm.tree_router import PrefixTree
from component_3_unified_parser.core.logparser_llm.llm_extractor import LLMExtractor, is_valid_template
from component_3_unified_parser.core.logparser_llm.template_manager import (
    TemplateManager,
    jaccard_token_similarity,
)

# Explicit imports so `core.X` absolute imports (used by main_parser.py internally)
# resolve to the same module instances @patch targets below -- this repo's
# pytest.ini puts both "." and "component_3_unified_parser" on pythonpath, which
# can otherwise create a phantom duplicate module (see test_component_3_persist.py).
import core.logparser_llm.llm_extractor
from component_3_unified_parser.main_parser import run_logparser_llm


def _write_config(config_dict):
    fd, path = tempfile.mkstemp(suffix='.yaml')
    with os.fdopen(fd, 'w') as f:
        yaml.safe_dump(config_dict, f)
    return path


class TestPromptModeOriginal(unittest.TestCase):
    """Point 4: paper's Figure 4 plain-text substitution prompt vs our JSON prompt."""

    def _make_extractor(self, prompt_mode='production'):
        config_path = _write_config({'logparser_llm': {'prompt_mode': prompt_mode, 'categories_mode': 'paper_10'}})
        self.addCleanup(os.remove, config_path)
        tree = PrefixTree()
        return LLMExtractor(tree, config_path=config_path)

    def test_default_is_production(self):
        extractor = self._make_extractor()
        self.assertEqual(extractor.prompt_mode, 'production')

    def test_original_prompt_has_no_json_instruction(self):
        extractor = self._make_extractor('original')
        extractor.llm_client.generate_completion = MagicMock(return_value='ignored')
        extractor.get_template('User admin logged in')
        messages = extractor.llm_client.generate_completion.call_args[0][0]
        sys_prompt = messages[0]['content']
        self.assertNotIn('JSON', sys_prompt)
        self.assertIn('Object ID (OID)', sys_prompt)  # paper's exact Figure 4 wording
        self.assertIn('Parsed Log:', messages[1]['content'])

    def test_production_prompt_unchanged(self):
        extractor = self._make_extractor('production')
        extractor.llm_client.generate_completion = MagicMock(return_value='{"template": "x", "variables": []}')
        extractor.get_template('User admin logged in')
        messages = extractor.llm_client.generate_completion.call_args[0][0]
        self.assertIn('JSON', messages[0]['content'])
        self.assertIn('Output:', messages[1]['content'])

    def test_original_response_used_directly_as_template(self):
        extractor = self._make_extractor('original')
        extractor.llm_client.generate_completion = MagicMock(return_value='User <LOI> logged in')
        template = extractor.get_template('User 1.2.3.4 logged in')
        self.assertEqual(template, 'User <LOI> logged in')

    def test_original_strips_parsed_log_echo_and_quotes(self):
        extractor = self._make_extractor('original')
        extractor.llm_client.generate_completion = MagicMock(return_value='Parsed Log: "User <LOI> logged in"')
        template = extractor.get_template('User 1.2.3.4 logged in')
        self.assertEqual(template, 'User <LOI> logged in')

    def test_original_mode_ecs_mapping_reconstructed_without_json(self):
        """No JSON "variables" list is emitted under prompt_mode="original" -- ECS
        mapping must come from get_variables_from_example() instead."""
        extractor = self._make_extractor('original')
        extractor.llm_client.generate_completion = MagicMock(return_value='User <LOI> logged in')
        record = {}
        extractor.get_template('User 1.2.3.4 logged in', record)
        self.assertEqual(record.get('source', {}).get('ip'), '1.2.3.4')

    def test_original_prompt_has_anti_preamble_instruction(self):
        """Disclosed adaptation, not in the paper's Figure 4 text: local Ollama
        models routinely preface answers with prose the paper's GPT-4/3.5-tuned
        prompt never needed to guard against -- found via a live 10-min run
        against real botsv3 data, where responses like "It seems like you've
        provided a log entry..." leaked straight through as the "template"."""
        extractor = self._make_extractor('original')
        extractor.llm_client.generate_completion = MagicMock(return_value='ignored')
        extractor.get_template('User admin logged in')
        sys_prompt = extractor.llm_client.generate_completion.call_args[0][0][0]['content']
        self.assertIn('conversational preamble', sys_prompt)

    def test_original_mode_falls_back_to_literal_on_preamble_response(self):
        extractor = self._make_extractor('original')
        extractor.llm_client.generate_completion = MagicMock(
            return_value="It seems like you've provided a log entry and an attempt to parse it, "
                         "but the parsing result is incomplete or unclear."
        )
        template = extractor.get_template('User admin logged in')
        self.assertEqual(template, 'User admin logged in')


class TestIsValidTemplatePreambleDetection(unittest.TestCase):
    """Regression guard for the botsv3-live-run finding: conversational preamble
    from local models leaking through as a "template" under prompt_mode="original"."""

    def test_rejects_observed_preamble_responses(self):
        observed = [
            "It seems like you've provided a log entry and an attempt to parse it, "
            "but the parsing result is incomplete or unclear.",
            "It seems like you're working on parsing logs and have provided some examples.",
            "Here is the parsed log: User <LOI> logged in",
            "Sure, I can help with that. User <LOI> logged in",
        ]
        for response in observed:
            with self.subTest(response=response):
                self.assertFalse(is_valid_template(response))

    def test_accepts_real_templates(self):
        real = [
            'User <LOI> logged in',
            '{"endtime":"<TDA>","timestamp":"<TDA>","bytes":"<CRS>"}',
            '<OID> <OID> eni-<OID> <LOI> <LOI> <SID> <SID>',
        ]
        for template in real:
            with self.subTest(template=template):
                self.assertTrue(is_valid_template(template))


class TestJaccardTokenSimilarity(unittest.TestCase):
    def test_identical_templates(self):
        self.assertEqual(jaccard_token_similarity('a b c', 'a b c'), 1.0)

    def test_disjoint_templates(self):
        self.assertEqual(jaccard_token_similarity('a b c', 'x y z'), 0.0)

    def test_partial_overlap_independent_of_length(self):
        # 2 shared tokens out of 4 unique total, despite different lengths
        sim = jaccard_token_similarity('a b c', 'a b')
        self.assertAlmostEqual(sim, 2 / 3)


class TestMergeModeOriginal(unittest.TestCase):
    """Point 2: paper's Figure 6/7 LLM-driven merge vs our structural substitution."""

    def _make_manager(self, merge_mode='production', llm_client=None, **overrides):
        cfg = {'merge_mode': merge_mode}
        cfg.update(overrides)
        config_path = _write_config({'logparser_llm': cfg})
        self.addCleanup(os.remove, config_path)
        tree = PrefixTree()
        return TemplateManager(tree, config_path=config_path, llm_client=llm_client)

    def test_default_is_production(self):
        manager = self._make_manager()
        self.assertEqual(manager.merge_mode, 'production')

    def test_try_merge_pair_structural_matches_and_substitutes(self):
        manager = self._make_manager('production')
        manager.merge_similarity_threshold = 0.75
        result = manager.try_merge_pair('User admin logged in', 'User system logged in')
        self.assertEqual(result, 'User <*> logged in')

    def test_try_merge_pair_structural_rejects_different_length(self):
        """The concrete, documented gap in the structural approach: it can never
        even compare templates of different token counts."""
        manager = self._make_manager('production')
        result = manager.try_merge_pair('User admin logged in', 'User admin logged in now')
        self.assertIsNone(result)

    def test_try_merge_pair_llm_check_no_closes_different_length_gap(self):
        """Unlike structural mode, merge_mode="original" is not restricted to
        equal-length pairs -- the LLM judges semantic equivalence directly."""
        mock_client = MagicMock()
        mock_client.generate_completion.side_effect = [
            'yes',  # check
            'User <*> logged in',  # verify -- unified template
        ]
        manager = self._make_manager('original', llm_client=mock_client, merge_prefilter_threshold=0.3)
        result = manager.try_merge_pair('User admin logged in', 'User admin logged in now')
        self.assertEqual(result, 'User <*> logged in')

    def test_try_merge_pair_llm_check_says_no(self):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = 'no'
        manager = self._make_manager('original', llm_client=mock_client, merge_prefilter_threshold=0.3)
        # Enough token overlap to clear the prefilter ("User admin logged" shared)
        # but different enough that the check should plausibly say no in real use.
        result = manager.try_merge_pair('User admin logged in', 'User admin logged out successfully')
        self.assertIsNone(result)
        # Only the check prompt should have been called -- no point verifying
        # a merge the check already rejected.
        self.assertEqual(mock_client.generate_completion.call_count, 1)

    def test_try_merge_pair_llm_prefilter_skips_llm_entirely_for_dissimilar_pair(self):
        """Scale guard: pairs that don't clear merge_prefilter_threshold never
        reach the LLM at all -- bounds call volume the way historical_variables_cap
        bounded LogBatcher's blowup."""
        mock_client = MagicMock()
        manager = self._make_manager('original', llm_client=mock_client, merge_prefilter_threshold=0.9)
        result = manager.try_merge_pair('User admin logged in', 'Connection from host closed')
        self.assertIsNone(result)
        mock_client.generate_completion.assert_not_called()

    def test_calibrate_dispatches_to_llm_path_when_original(self):
        mock_client = MagicMock()
        mock_client.generate_completion.side_effect = ['yes', 'User <*> logged in']
        manager = self._make_manager('original', llm_client=mock_client, merge_prefilter_threshold=0.3)
        manager.tree_router.clusters = ['User admin logged in', 'User system logged in']
        for t in manager.tree_router.clusters:
            manager.tree_router.insert(t)

        manager.calibrate()

        self.assertEqual(manager.tree_router.clusters, ['User <*> logged in'])

    def test_verify_returns_none_on_none_response(self):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = 'None'
        manager = self._make_manager('original', llm_client=mock_client)
        result = manager._llm_merge_verify('User admin logged in', 'Connection from host closed')
        self.assertIsNone(result)


class TestGetLooseMatchCandidates(unittest.TestCase):
    """Point 1 support: PrefixTree.get_loose_match_candidates() exposes the
    candidate set that match_llm_mode="original" merge-checks against, unlike
    loose_match() which collapses to a single winner-or-None decision."""

    def test_returns_multiple_candidates_sorted_by_score(self):
        tree = PrefixTree()
        tree.loose_match_threshold = 0.5
        tree.clusters = [
            'Connection from host1 closed',
            'Connection from host2 closed',
            'Totally different unrelated message',
        ]
        candidates = tree.get_loose_match_candidates(['Connection', 'from', 'hostX', 'closed'])
        self.assertEqual(len(candidates), 2)
        self.assertIn('Connection from host1 closed', candidates)
        self.assertIn('Connection from host2 closed', candidates)

    def test_returns_empty_when_nothing_clears_threshold(self):
        tree = PrefixTree()
        tree.loose_match_threshold = 0.99
        tree.clusters = ['Completely unrelated template here']
        candidates = tree.get_loose_match_candidates(['Connection', 'from', 'host', 'closed'])
        self.assertEqual(candidates, [])

    def test_does_not_update_last_matched(self):
        """Read-only: identifying a candidate isn't the same as using it."""
        tree = PrefixTree()
        tree.loose_match_threshold = 0.5
        tree.insert('Connection from host1 closed')
        node = tree.root.children['Connection'].children['from'].children['host1'].children['closed']
        before = node.last_matched
        tree.get_loose_match_candidates(['Connection', 'from', 'hostX', 'closed'])
        self.assertEqual(node.last_matched, before)


class TestMatchLlmModeOriginal(unittest.TestCase):
    """Point 1 end-to-end: paper's Algorithm 1 -- only a strict match skips the
    LLM; a loose match still queries it, unlike match_llm_mode="production" where
    a loose match is itself a full, unverified cache hit."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.output_dir = os.path.join(self.test_dir, 'output')
        os.makedirs(self.output_dir)
        self.input_file = os.path.join(self.test_dir, 'loghub_ecs.jsonl')
        # Two 6-token logs, same token count, differing only in the LAST token.
        # PrefixTree/LLMExtractor/TemplateManager all load their own config
        # independently (run_logparser_llm constructs them with no config_path
        # override), so they fall back to their hardcoded defaults in this test
        # environment (no /app/config.yaml) regardless of what's mocked for
        # load_config() below -- loose_match_metric defaults to "positional_decay"
        # (decay_factor 0.15) and loose_match_threshold to 0.8. A mismatch only at
        # the last of 6 positions scores ~0.889 under that metric (early positions
        # carry most of the weight), comfortably clearing the real default
        # threshold without needing to override it.
        with open(self.input_file, 'w', encoding='utf-8') as f:
            f.write(json.dumps({"message": "Connection from host alpha beta closed1", "event": {"id": "1"}}) + '\n')
            f.write(json.dumps({"message": "Connection from host alpha beta closed2", "event": {"id": "2"}}) + '\n')

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    @patch('component_3_unified_parser.main_parser.load_config')
    @patch('core.logparser_llm.llm_extractor.OllamaClient')
    def test_production_loose_match_skips_second_llm_call(self, mock_client_class, mock_load_config):
        mock_client = MagicMock()
        # First call returns the log verbatim (no generalization) so the tree
        # holds a literal template with no <*>, making the second line a loose
        # match candidate rather than a strict wildcard match.
        mock_client.generate_completion.return_value = 'Connection from host alpha beta closed1'
        mock_client.get_usage.return_value = {"invocations": 1, "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        mock_client_class.return_value = mock_client
        mock_load_config.return_value = {
            'directories': {'dataset_name': 'loghub'},
            'logparser_llm': {'match_llm_mode': 'production'},
        }

        run_logparser_llm(input_files=[self.input_file], output_dir=self.output_dir)

        self.assertEqual(mock_client.generate_completion.call_count, 1)

    @patch('component_3_unified_parser.main_parser.load_config')
    @patch('core.logparser_llm.llm_extractor.OllamaClient')
    def test_original_loose_match_still_calls_llm(self, mock_client_class, mock_load_config):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = 'Connection from host alpha beta closed1'
        mock_client.get_usage.return_value = {"invocations": 1, "prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        mock_client_class.return_value = mock_client
        mock_load_config.return_value = {
            'directories': {'dataset_name': 'loghub'},
            'logparser_llm': {'match_llm_mode': 'original', 'merge_mode': 'production'},
        }

        run_logparser_llm(input_files=[self.input_file], output_dir=self.output_dir)

        # Strict match never fires (no <*> in the tree yet), so BOTH lines must
        # reach the LLM under "original" -- the defining behavioral difference
        # from match_llm_mode="production" above.
        self.assertEqual(mock_client.generate_completion.call_count, 2)


if __name__ == '__main__':
    unittest.main()
