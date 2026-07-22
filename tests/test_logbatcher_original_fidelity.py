import re
import time
import signal
import unittest
from unittest.mock import MagicMock, patch

from component_3_unified_parser.core.logbatcher.postprocess import (
    correct_single_template,
    exclude_digits,
    verify_template,
    apply_original_postprocessing,
)
from component_3_unified_parser.core.logbatcher.parsing_base import ParsingBase
from component_3_unified_parser.core.logbatcher.sample import (
    SimilarSampler,
    DPPSampler,
    _group_samples_clustering,
    _greedy_dpp_select,
    get_sampler,
)
from component_3_unified_parser.core.logbatcher.parser import LogBatcher, not_varibility, _guarded_cluster_match
import component_3_unified_parser.core.logbatcher.parser as logbatcher_parser_module
from component_3_unified_parser.core.logbatcher.matching import template_to_regex

# @patch('core.logbatcher.parser.X') resolves to a *separate* duplicate
# module instance in this repo's dual-pythonpath test setup --
# @patch.object(logbatcher_parser_module, 'X') targets the real module.


class TestCorrectSingleTemplate(unittest.TestCase):
    def test_boolean_substitution(self):
        result = correct_single_template('Connection from <*> closed by port true')
        self.assertIn('<*>', result)
        self.assertNotIn('true', result)

    def test_path_like_string_collapsed(self):
        result = correct_single_template('Reading file /var/log/syslog.1.gz for user root')
        self.assertNotIn('/var/log', result)
        self.assertNotIn('root', result)

    def test_consecutive_variables_collapsed(self):
        result = correct_single_template('value <*><*> end')
        self.assertNotIn('<*><*>', result)

    def test_size_unit_collapsed(self):
        result = correct_single_template('File size is <*> KB total')
        self.assertNotIn('KB', result)


class TestExcludeDigits(unittest.TestCase):
    def test_long_digit_run_flagged(self):
        self.assertTrue(exclude_digits('12345'))

    def test_alpha_leading_token_not_flagged(self):
        self.assertFalse(exclude_digits('abc123'))

    def test_short_low_ratio_digit_token_not_flagged(self):
        self.assertFalse(exclude_digits('a1'))


class TestVerifyTemplate(unittest.TestCase):
    def test_degenerate_template_rejected(self):
        self.assertFalse(verify_template('<*> <*>'))

    def test_real_template_accepted(self):
        self.assertTrue(verify_template('User <*> logged in'))


class TestApplyOriginalPostprocessing(unittest.TestCase):
    def test_falls_back_to_raw_log_on_degenerate_output(self):
        result = apply_original_postprocessing('<*> <*>', 'fallback raw log 42')
        self.assertNotEqual(result.replace('<*>', '').replace(' ', ''), '')
        self.assertIn('<*>', result)  # the digit "42" gets normalized too

    def test_keeps_valid_output(self):
        result = apply_original_postprocessing('User <*> logged in', 'irrelevant fallback')
        self.assertEqual(result, 'User <*> logged in')


class TestNotVaribility(unittest.TestCase):
    def test_uniform_except_digits_is_true(self):
        logs = ['User bob123 logged in', 'User bob456 logged in', 'User bob789 logged in']
        self.assertTrue(not_varibility(logs))

    def test_genuinely_diverse_is_false(self):
        logs = ['User bob logged in', 'Disk usage at 95 percent', 'Connection closed']
        self.assertFalse(not_varibility(logs))


class TestGroupSamplesClustering(unittest.TestCase):
    def test_returns_index_groups_within_batch_size(self):
        import numpy as np
        rng = np.random.RandomState(0)
        matrix = rng.rand(12, 5)
        groups = _group_samples_clustering(matrix, num_in_batch=4)
        for group in groups:
            self.assertLessEqual(len(group), 4)
        all_indices = sorted(i for group in groups for i in group)
        self.assertEqual(all_indices, list(range(12)))


class TestSimilarSamplerOriginalMode(unittest.TestCase):
    def test_original_mode_returns_batch_size_logs_no_embedding_calls(self):
        logs = [{"id": str(i), "message": f"User admin_{i} logged in from host{i % 3}"} for i in range(15)]
        sampler = SimilarSampler(batch_size=5, mode='original')
        result = sampler.sample(logs)
        self.assertLessEqual(len(result), 5)
        self.assertTrue(all(log in logs for log in result))

    def test_production_mode_unchanged(self):
        logs = [{"id": str(i), "message": f"msg {i}"} for i in range(10)]
        sampler = SimilarSampler(batch_size=4, mode='production')
        result = sampler.sample(logs)
        self.assertEqual(len(result), 4)


class TestOriginalPrompt(unittest.TestCase):
    def test_original_prompt_contains_taxonomy_and_historical_variables(self):
        pb = ParsingBase(llm_client=MagicMock())
        messages = pb._build_original_prompt(
            [{"message": "User bob logged in"}], historical_variables=['bob', 'alice']
        )
        system_content = messages[0]['content']
        self.assertIn('IPv4_port', system_content)
        self.assertIn('Historical variables', system_content)
        self.assertIn("['bob', 'alice']", system_content)

    def test_production_prompt_has_worked_example(self):
        pb = ParsingBase(llm_client=MagicMock())
        messages = pb._build_production_prompt([{"message": "x"}])
        self.assertIn('Example logs', messages[0]['content'])

    def test_convert_original_placeholders(self):
        pb = ParsingBase(llm_client=MagicMock())
        result = pb._convert_original_placeholders('User `{{user}}` logged in from ${ip}')
        self.assertEqual(result, 'User `<*>` logged in from <*>')

    def test_batch_query_original_mode_converts_and_cleans(self):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = '`User {{user}} logged in`'
        pb = ParsingBase(llm_client=mock_client)
        result = pb.batch_query([{"message": "User bob logged in"}], prompt_mode='original')
        self.assertEqual(result, 'User <*> logged in')

    def test_extract_original_response_strips_conversational_preamble(self):
        """Regression guard: a live run against real logs found the local
        model (unlike the GPT-4o-mini upstream was written against) routinely
        prefaces its answer with prose before the backtick-delimited template
        -- e.g. "The log messages contain a consistent structure ... \n```\n
        BLOCK* NameSystem...\n```". clean_template() (our own markdown
        stripper) doesn't handle this; upstream's real extraction (find
        first/last backtick, take the longest non-degenerate segment between
        them) does. This is the exact case that surfaced the bug."""
        pb = ParsingBase(llm_client=MagicMock())
        response = (
            "The log messages contain a consistent structure with placeholders for "
            "IP addresses and block IDs. The template should abstract these variables "
            "while keeping the constant text intact.\n\n"
            "```\nBLOCK* NameSystem.addStoredBlock: blockMap updated: {{ip}} is added to blk_{{id}} size {{size}}\n```"
        )
        result = pb._extract_original_response(response)
        self.assertNotIn('The log messages contain', result)
        self.assertIn('BLOCK* NameSystem.addStoredBlock', result)

    def test_extract_original_response_simple_case(self):
        pb = ParsingBase(llm_client=MagicMock())
        result = pb._extract_original_response('`User {{name}} logged in`')
        self.assertEqual(result, 'User {{name}} logged in')

    def test_extract_original_response_no_backticks_returns_empty(self):
        pb = ParsingBase(llm_client=MagicMock())
        result = pb._extract_original_response('No backticks here at all')
        self.assertEqual(result, '')


class TestLogBatcherFidelityModeWiring(unittest.TestCase):
    def test_defaults_are_production(self):
        parser = LogBatcher('/app/config.yaml')  # missing in this env -> config = {} -> defaults
        self.assertEqual(parser.postprocess_mode, 'production')
        self.assertEqual(parser.prompt_mode, 'production')
        self.assertEqual(parser.batch_truncation_mode, 'production')
        self.assertEqual(parser._variable_candidates, [])

        # sampler_type defaults to DPPSampler, which has no .mode concept --
        # SimilarSampler's default mode is checked separately below.
        similar_sampler = SimilarSampler()
        self.assertEqual(similar_sampler.mode, 'production')

    @patch.object(logbatcher_parser_module, 'get_sampler')
    @patch.object(logbatcher_parser_module, 'OllamaClient')
    def test_similar_sampler_mode_reaches_get_sampler(self, mock_ollama_client_class, mock_get_sampler):
        import tempfile, os, yaml
        mock_ollama_client_class.return_value = MagicMock()
        mock_get_sampler.return_value = MagicMock()

        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'similar_sampler_mode': 'original'}}, f)
        self.addCleanup(os.remove, path)

        LogBatcher(path)
        _, kwargs = mock_get_sampler.call_args
        self.assertEqual(kwargs.get('similar_sampler_mode'), 'original')


class TestUpdateVariableCandidates(unittest.TestCase):
    def test_purely_alpha_value_filtered_out(self):
        parser = LogBatcher('/app/config.yaml')
        parser._update_variable_candidates('User bob logged in', 'User <*> logged in')
        # "bob" is purely alphabetic -> filtered (upstream only keeps mixed
        # alphanumeric/punctuated values as "variables").
        self.assertNotIn('bob', parser._variable_candidates)

    def test_mixed_alphanumeric_value_kept(self):
        parser = LogBatcher('/app/config.yaml')
        parser._update_variable_candidates('Connecting host-42 to port', 'Connecting <*> to port')
        self.assertIn('host-42', parser._variable_candidates)

    def test_no_duplicate_candidates(self):
        parser = LogBatcher('/app/config.yaml')
        parser._update_variable_candidates('Connecting host-42 to port', 'Connecting <*> to port')
        parser._update_variable_candidates('Connecting host-42 to port', 'Connecting <*> to port')
        self.assertEqual(parser._variable_candidates.count('host-42'), 1)


class TestGeneratedTemplateBacktickCleaning(unittest.TestCase):
    """Regression guard: generated_template used to be used for
    parsed_results/cache.add/variable extraction *before* backtick cleanup --
    dormant under prompt_mode="production" (whose prompt forbids markdown)
    but a real bug once prompt_mode="original" asks for backtick-delimited
    output. Fixed by clean_template()-ing generated_template immediately
    after the postprocess_mode branch, in both cache-miss handlers."""

    @patch.object(logbatcher_parser_module, 'OllamaClient')
    def test_production_cache_mode_matches_despite_backticks(self, mock_ollama_client_class):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = '`User {{user}} logged in`'
        mock_client.get_usage.return_value = {}
        mock_ollama_client_class.return_value = mock_client

        import tempfile, os, yaml
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'prompt_mode': 'original', 'cluster': 'LengthCluster', 'sampler': 'RandomSampler'}}, f)
        self.addCleanup(os.remove, path)

        parser = LogBatcher(path)
        logs = [{"id": str(i), "message": f"User bob{i} logged in"} for i in range(5)]
        results = parser.parse(logs)
        self.assertEqual(len(results), 5)
        self.assertEqual(results[0]['template'], 'User <*> logged in')
        self.assertIn('bob0', parser._variable_candidates)

    @patch.object(logbatcher_parser_module, 'OllamaClient')
    def test_original_cache_mode_matches_despite_backticks(self, mock_ollama_client_class):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = '`User {{user}} logged in`'
        mock_client.get_usage.return_value = {}
        mock_ollama_client_class.return_value = mock_client

        import tempfile, os, yaml
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'prompt_mode': 'original', 'cache_mode': 'original', 'cluster': 'LengthCluster', 'sampler': 'RandomSampler'}}, f)
        self.addCleanup(os.remove, path)

        parser = LogBatcher(path)
        logs = [{"id": str(i), "message": f"User bob{i} logged in"} for i in range(5)]
        results = parser.parse(logs)
        self.assertEqual(len(results), 5)
        self.assertEqual(results[0]['template'], 'User <*> logged in')


class TestGreedyDppSelect(unittest.TestCase):
    def test_picks_diverse_pair(self):
        import numpy as np
        # Index 0 and 1 are near-identical (high similarity); index 2 is
        # dissimilar from both -- diverse selection should pick 0 (or 1) then 2.
        kernel = np.array([
            [1.0, 0.9, 0.1],
            [0.9, 1.0, 0.2],
            [0.1, 0.2, 1.0],
        ])
        idxs = _greedy_dpp_select(kernel, 2)
        self.assertEqual(len(idxs), 2)
        self.assertIn(2, idxs)  # the dissimilar one must be picked

    def test_k_larger_than_n_returns_all(self):
        import numpy as np
        kernel = np.eye(2)
        idxs = _greedy_dpp_select(kernel, 5)
        self.assertEqual(sorted(idxs), [0, 1])


class TestDPPSamplerKernelMode(unittest.TestCase):
    def test_original_kernel_no_embedding_calls_even_for_long_logs(self):
        mock_client = MagicMock()
        sampler = DPPSampler(mock_client, batch_size=5, dpp_kernel_mode='original')
        # Deliberately long messages that would trigger the length-threshold
        # Jaccard fallback under dpp_kernel_mode="production".
        logs = [{"id": str(i), "message": f"User admin_{i} logged in " + "x" * 5000} for i in range(20)]
        result = sampler.sample(logs)
        self.assertEqual(len(result), 5)
        mock_client.get_embedding.assert_not_called()

    def test_production_kernel_unchanged(self):
        mock_client = MagicMock()
        mock_client.get_embedding.side_effect = lambda msg: [hash(msg) % 100, len(msg)]
        sampler = DPPSampler(mock_client, batch_size=3, dpp_kernel_mode='production')
        logs = [{"id": str(i), "message": f"short log {i}"} for i in range(10)]
        result = sampler.sample(logs)
        self.assertEqual(len(result), 3)
        self.assertTrue(mock_client.get_embedding.called)

    def test_get_sampler_passes_dpp_kernel_mode_through(self):
        mock_client = MagicMock()
        sampler = get_sampler("DPPSampler", mock_client, batch_size=5, dpp_kernel_mode='original')
        self.assertEqual(sampler.dpp_kernel_mode, 'original')


class TestLogBatcherDppKernelModeWiring(unittest.TestCase):
    def test_default_is_production(self):
        parser = LogBatcher('/app/config.yaml')  # missing in this env -> config = {} -> default
        self.assertIsInstance(parser.sampler, DPPSampler)  # default sampler_type
        self.assertEqual(parser.sampler.dpp_kernel_mode, 'production')

    def test_explicit_original_mode_reaches_sampler(self):
        import tempfile, os, yaml
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'dpp_kernel_mode': 'original'}}, f)
        self.addCleanup(os.remove, path)

        parser = LogBatcher(path)
        self.assertEqual(parser.sampler.dpp_kernel_mode, 'original')


class TestHistoricalVariablesCap(unittest.TestCase):
    """Regression guard for the historical_variables_cap adaptation: upstream
    includes the entire unbounded variable_candidates list in every prompt;
    benchmarked directly against real logs this made a full-loghub run's
    cumulative time grow roughly quadratically with call count. This caps the
    prompt to the most recent N entries instead."""

    def test_default_is_uncapped(self):
        parser = LogBatcher('/app/config.yaml')  # missing in this env -> config = {} -> default
        self.assertIsNone(parser.historical_variables_cap)
        parser._variable_candidates = [f'v{i}' for i in range(500)]
        self.assertEqual(len(parser._get_historical_variables()), 500)

    def test_cap_returns_most_recent_n(self):
        import tempfile, os, yaml
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'historical_variables_cap': 10}}, f)
        self.addCleanup(os.remove, path)

        parser = LogBatcher(path)
        self.assertEqual(parser.historical_variables_cap, 10)
        parser._variable_candidates = [f'v{i}' for i in range(30)]
        result = parser._get_historical_variables()
        self.assertEqual(len(result), 10)
        self.assertEqual(result, [f'v{i}' for i in range(20, 30)])  # most recent 10

    def test_cap_no_effect_when_list_smaller_than_cap(self):
        import tempfile, os, yaml
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'historical_variables_cap': 100}}, f)
        self.addCleanup(os.remove, path)

        parser = LogBatcher(path)
        parser._variable_candidates = ['a', 'b', 'c']
        self.assertEqual(parser._get_historical_variables(), ['a', 'b', 'c'])

    def test_update_variable_candidates_trims_underlying_list_when_capped(self):
        """_update_variable_candidates's own `val not in self._variable_candidates`
        scan (faithful port of upstream vars_update()'s `var not in candidates`)
        is O(n) against the full list -- found via a live botsv3 run where
        throughput decayed from 153.7 to 16.0 logs/s over 2 hours even with
        historical_variables_cap set, because the cap only bounded the
        prompt-facing slice, not this scan or the list's own growth. The list
        itself must stay trimmed to the cap so the scan stays cheap."""
        import tempfile, os, yaml
        fd, path = tempfile.mkstemp(suffix='.yaml')
        with os.fdopen(fd, 'w') as f:
            yaml.safe_dump({'logbatcher': {'historical_variables_cap': 5}}, f)
        self.addCleanup(os.remove, path)

        parser = LogBatcher(path)
        parser._variable_candidates = [f'v{i}' for i in range(5)]
        parser._update_variable_candidates('user bob logged in from 10.0.0.1', 'user <*> logged in from <*>')
        self.assertLessEqual(len(parser._variable_candidates), 5)
        self.assertEqual(parser._variable_candidates[-1], '10.0.0.1')

    def test_update_variable_candidates_uncapped_list_keeps_growing(self):
        parser = LogBatcher('/app/config.yaml')  # missing -> uncapped default
        parser._variable_candidates = [f'v{i}' for i in range(500)]
        parser._update_variable_candidates('connection from 10.0.0.1 refused', 'connection from <*> refused')
        self.assertEqual(len(parser._variable_candidates), 501)
        self.assertEqual(parser._variable_candidates[-1], '10.0.0.1')


class TestTemplateToRegexCollapsesDelimiterJoinedPlaceholders(unittest.TestCase):
    """Regression tests for the botsv3 ReDoS hang (2026-07-21): an LLM template
    that captured a JSON array element-by-element (e.g. a 21-element SSL cipher
    list) produced dozens of separate <*> placeholders joined by a short
    literal delimiter (","), not whitespace. template_to_regex()'s original
    collapse only handled whitespace-joined runs, so these slipped through
    uncollapsed and could catastrophically backtrack when matched against a
    non-conforming log. See parser.py::_guarded_cluster_match's docstring for
    the live-hang writeup."""

    def test_comma_joined_placeholders_collapse_to_one_group(self):
        pattern = template_to_regex('[<*>,<*>,<*>]')
        self.assertEqual(pattern.pattern.count('(.*?)'), 1)
        self.assertTrue(pattern.match('[1,22,333]'))

    def test_whitespace_joined_placeholders_still_collapse(self):
        # Pre-existing mitigation -- must not regress.
        pattern = template_to_regex('<*> <*> <*> logged in')
        self.assertEqual(pattern.pattern.count('(.*?)'), 1)
        self.assertTrue(pattern.match('a b c logged in'))

    def test_wide_array_template_collapses_and_matches_quickly(self):
        # Structurally the same shape as the real botsv3 template that hung
        # (a 21-element cipher-list array captured field-by-field), without
        # depending on the actual captured debug artifact from that run.
        array_template = '{"cipher_list":[' + ','.join(['<*>'] * 21) + ']}'
        pattern = template_to_regex(array_template)
        self.assertEqual(pattern.pattern.count('(.*?)'), 1)

        array_log = '{"cipher_list":[' + ','.join(str(i) for i in range(21)) + ']}'
        start = time.time()
        matched = pattern.match(array_log)
        elapsed = time.time() - start
        self.assertTrue(matched)
        self.assertLess(elapsed, 1.0)

    def test_placeholders_separated_by_real_field_text_are_not_over_collapsed(self):
        # Two distinct JSON fields ("a" and "b") must stay as two separate
        # capture groups -- only genuinely adjacent, delimiter-joined <*> runs
        # (no intervening word characters) should collapse.
        pattern = template_to_regex('{"a":<*>,"b":<*>}')
        self.assertEqual(pattern.pattern.count('(.*?)'), 2)
        match = pattern.match('{"a":1,"b":2}')
        self.assertTrue(match)
        self.assertEqual(match.groups(), ('1', '2'))


class TestGuardedClusterMatchTimeout(unittest.TestCase):
    """Regression tests for the timeout guard added to parser.py's two
    template_to_regex(...).match() call sites that previously had none
    (unlike matching.py::match_log()/original_cache.py::safe_search(), which
    already used this same signal.alarm mitigation). Without it, a pathological
    template tested against a non-conforming log could backtrack forever and
    freeze the single-threaded pipeline -- this is what actually hung the live
    botsv3 run."""

    def test_matching_logs_are_returned(self):
        pattern = template_to_regex('user <*> logged in')
        cluster = [{'id': '1', 'message': 'user bob logged in'}, {'id': '2', 'message': 'user alice logged in'}]
        matched = _guarded_cluster_match(pattern, cluster)
        self.assertEqual({m['id'] for m in matched}, {'1', '2'})

    def test_non_matching_log_returns_empty_without_hanging(self):
        pattern = template_to_regex('user <*> logged in')
        cluster = [{'id': '1', 'message': 'connection refused'}]
        start = time.time()
        matched = _guarded_cluster_match(pattern, cluster, timeout=1)
        elapsed = time.time() - start
        self.assertEqual(matched, [])
        self.assertLess(elapsed, 1.0)

    def test_catastrophic_backtracking_pattern_times_out_instead_of_hanging(self):
        # Classic ReDoS shape (a+)+b, bypassing template_to_regex's own
        # collapse mitigation entirely, to prove the timeout guard itself --
        # not just the collapse -- is what actually bounds worst-case time.
        evil_pattern = re.compile(r'^(a+)+b$')
        evil_cluster = [{'id': '1', 'message': 'a' * 35 + 'c'}]  # no trailing 'b' -> forces full backtracking
        start = time.time()
        matched = _guarded_cluster_match(evil_pattern, evil_cluster, timeout=1)
        elapsed = time.time() - start
        self.assertEqual(matched, [])
        self.assertLess(elapsed, 2.0)  # bounded by the 1s alarm, not the exponential backtrack

    def test_restores_previous_sigalrm_handler(self):
        def sentinel_handler(signum, frame):
            pass
        old = signal.signal(signal.SIGALRM, sentinel_handler)
        try:
            pattern = template_to_regex('user <*> logged in')
            _guarded_cluster_match(pattern, [{'id': '1', 'message': 'user bob logged in'}])
            self.assertIs(signal.getsignal(signal.SIGALRM), sentinel_handler)
        finally:
            signal.signal(signal.SIGALRM, old)


if __name__ == '__main__':
    unittest.main()
