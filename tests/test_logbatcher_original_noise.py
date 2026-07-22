import unittest
from unittest.mock import MagicMock, patch

from component_3_unified_parser.core.logbatcher.additional_cluster import (
    SimilarityCluster,
    _reassign_noise_labels,
    _tokenize_for_noise_grouping,
)
from component_3_unified_parser.core.logbatcher.cluster import get_clusterer, LengthCluster
from component_3_unified_parser.core.logbatcher.parser import LogBatcher
import component_3_unified_parser.core.logbatcher.parser as logbatcher_parser_module

# @patch('core.logbatcher.parser.X') resolves to a *separate* duplicate
# module instance in this repo's dual-pythonpath test setup
# (component_3_unified_parser is on sys.path both directly and via the
# repo-root dotted import) -- it silently never touches the LogBatcher class
# actually under test here. @patch.object(logbatcher_parser_module, 'X')
# targets the real, correctly-resolved module object instead. Confirmed
# empirically: `core.logbatcher.parser.LogBatcher is
# component_3_unified_parser.core.logbatcher.parser.LogBatcher` is False.


class TestReassignNoiseLabels(unittest.TestCase):
    def test_exact_duplicates_merged_into_shared_label(self):
        # "connection from 10.0.0.1" and "connection from 10.0.0.2" tokenize
        # identically once digits are stripped -> exact duplicates.
        messages = ["connection from 10.0.0.1", "connection from 10.0.0.2", "totally unrelated log"]
        labels = [-1, -1, -1]
        new_labels, next_id = _reassign_noise_labels(labels, cluster_nums=0, messages=messages)

        self.assertNotIn(-1, new_labels)
        self.assertEqual(new_labels[0], new_labels[1])  # duplicates share a label
        self.assertNotEqual(new_labels[0], new_labels[2])  # singleton gets its own
        self.assertEqual(next_id, 2)  # two distinct groups formed

    def test_singleton_gets_own_new_label(self):
        labels = [-1]
        new_labels, next_id = _reassign_noise_labels(labels, cluster_nums=3, messages=["unique log"])
        self.assertEqual(new_labels[0], 3)
        self.assertEqual(next_id, 4)

    def test_real_cluster_labels_untouched(self):
        labels = [0, -1, 0]
        new_labels, _ = _reassign_noise_labels(labels, cluster_nums=1, messages=["a", "b", "c"])
        self.assertEqual(new_labels[0], 0)
        self.assertEqual(new_labels[2], 0)
        self.assertNotEqual(new_labels[1], -1)


class TestSimilarityClusterNoiseMode(unittest.TestCase):
    def test_production_mode_leaves_noise_logs_populated(self):
        logs = [
            {"message": "aaaaaaaa"},
            {"message": "bbbbbbbb"},
            {"message": "cccccccc"},
        ]
        clusterer = SimilarityCluster(logs, threshold=0.99, noise_mode='production')
        clusterer.get_partitions()
        # High threshold (tight eps) with all-distinct short logs -> DBSCAN
        # noise under production mode.
        self.assertTrue(len(clusterer.noise_logs) > 0)

    def test_original_mode_empties_noise_logs(self):
        logs = [
            {"message": "aaaaaaaa"},
            {"message": "bbbbbbbb"},
            {"message": "cccccccc"},
        ]
        clusterer = SimilarityCluster(logs, threshold=0.99, noise_mode='original')
        partitions = clusterer.get_partitions()

        self.assertEqual(clusterer.noise_logs, [])
        # Every log must appear in exactly one returned partition.
        flattened = [log for part in partitions for log in part]
        self.assertEqual(len(flattened), len(logs))


class TestGetClustererNoiseModePassthrough(unittest.TestCase):
    def test_similarity_cluster_receives_noise_mode(self):
        clusterer = get_clusterer("SimilarityCluster", [{"message": "x"}], noise_mode='original')
        self.assertEqual(clusterer.noise_mode, 'original')

    def test_length_cluster_ignores_noise_mode_harmlessly(self):
        clusterer = get_clusterer("LengthCluster", [{"message": "x"}], noise_mode='original')
        self.assertIsInstance(clusterer, LengthCluster)


class TestLogBatcherNoiseModeWiring(unittest.TestCase):
    @patch.object(logbatcher_parser_module, 'OllamaClient')
    @patch.object(logbatcher_parser_module, 'get_sampler')
    @patch.object(logbatcher_parser_module, 'ParsingBase')
    def test_default_noise_mode_is_production(self, mock_parsing_base_class, mock_get_sampler, mock_ollama_client_class):
        mock_ollama_client_class.return_value = MagicMock()
        mock_get_sampler.return_value = MagicMock()
        mock_parsing_base_class.return_value = MagicMock()

        parser = LogBatcher('/app/config.yaml')  # missing in this env -> config = {} -> default
        self.assertEqual(parser.noise_mode, 'production')

    @patch.object(logbatcher_parser_module, 'get_clusterer')
    @patch.object(logbatcher_parser_module, 'OllamaClient')
    @patch.object(logbatcher_parser_module, 'get_sampler')
    @patch.object(logbatcher_parser_module, 'ParsingBase')
    def test_noise_mode_reaches_get_clusterer_call(self, mock_parsing_base_class, mock_get_sampler, mock_ollama_client_class, mock_get_clusterer):
        mock_ollama_client_class.return_value = MagicMock()
        mock_get_sampler.return_value = MagicMock()
        mock_parsing_base_class.return_value = MagicMock()
        mock_clusterer = MagicMock()
        mock_clusterer.get_partitions.return_value = []
        mock_clusterer.noise_logs = []
        mock_get_clusterer.return_value = mock_clusterer

        parser = LogBatcher('/app/config.yaml')
        parser.noise_mode = 'original'
        parser.cluster_type = 'SimilarityCluster'
        parser._last_progress_log_time = 0

        parsed_results = {}
        counters = {'log_volume': 0, 'llm_invocations': 0, 'cache_hits': 0}
        parser._process_micro_batch(
            [{"id": "1", "message": "hello"}], parsed_results, 'data/parsed/quarantine.jsonl',
            start_time=0, time_limit=None, history=[], counters=counters
        )

        _, kwargs = mock_get_clusterer.call_args
        self.assertEqual(kwargs.get('noise_mode'), 'original')


if __name__ == '__main__':
    unittest.main()
