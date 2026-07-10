import os
import time
import json
import unittest
import numpy as np
from unittest.mock import MagicMock, patch
from component_3_unified_parser.core.logbatcher.parser import LogBatcher, jaccard_similarity
from component_3_unified_parser.core.logbatcher.additional_cluster import SimilarityCluster
from component_3_unified_parser.core.logbatcher.parsing_cache import ParsingCache

class TestLogBatcherDBSCAN(unittest.TestCase):
    def setUp(self):
        self.config_path = '/app/config.yaml'
        self.quarantine_file = 'data/parsed/quarantine.jsonl'
        if os.path.exists(self.quarantine_file):
            try:
                os.remove(self.quarantine_file)
            except Exception:
                pass

    def tearDown(self):
        if os.path.exists(self.quarantine_file):
            try:
                os.remove(self.quarantine_file)
            except Exception:
                pass

    def test_jaccard_distance_matrix(self):
        logs = [
            {"message": "hello world"},
            {"message": "hello world"},
            {"message": "hello python"}
        ]
        clusterer = SimilarityCluster(logs, threshold=0.8)
        partitions = clusterer.get_partitions()

        # Distance matrix must be initialized
        self.assertIsNotNone(clusterer.dist_matrix)
        dist = clusterer.dist_matrix

        # Symmetric matrix assertions
        self.assertEqual(dist[0, 0], 0.0)
        self.assertEqual(dist[0, 1], 0.0)
        # Jaccard distance between "hello world" and "hello python" is 1 - 1/3 = 2/3
        self.assertAlmostEqual(dist[0, 2], 2.0 / 3.0)
        self.assertEqual(dist[2, 0], dist[0, 2])

    def test_medoid_extraction(self):
        logs = [
            {"message": "hello world"},
            {"message": "hello world python"},
            {"message": "hello world java"}
        ]
        clusterer = SimilarityCluster(logs, threshold=0.5)
        # Force distance matrix computation
        _ = clusterer.get_partitions()

        # Medoid of logs should be the one closest to all others ("hello world")
        medoid = clusterer.get_medoid(logs)
        self.assertEqual(medoid["message"], "hello world")

    def test_lru_cache_eviction(self):
        cache = ParsingCache(max_size=3)
        cache.add("t1", "raw1")
        cache.add("t2", "raw2")
        cache.add("t3", "raw3")

        self.assertEqual(len(cache._data), 3)

        # Evicts oldest (t1)
        cache.add("t4", "raw4")
        self.assertEqual(len(cache._data), 3)
        self.assertNotIn("t1", cache._data)

        # Access t2 (makes it MRU)
        cache.add("t2", "raw2")

        # Evicts oldest remaining (t3)
        cache.add("t5", "raw5")
        self.assertEqual(len(cache._data), 3)
        self.assertNotIn("t3", cache._data)
        self.assertIn("t2", cache._data)
        self.assertIn("t4", cache._data)
        self.assertIn("t5", cache._data)

    @patch('core.logbatcher.parser.OllamaClient')
    @patch('core.logbatcher.parser.get_sampler')
    @patch('core.logbatcher.parser.ParsingBase')
    def test_hybrid_trigger(self, mock_parsing_base_class, mock_get_sampler, mock_ollama_client_class):
        # Mock LLM and Sampler components
        mock_client = MagicMock()
        mock_ollama_client_class.return_value = mock_client
        mock_sampler = MagicMock()
        mock_get_sampler.return_value = mock_sampler
        mock_parsing_base = MagicMock()
        mock_parsing_base.batch_query.return_value = "Mock template"
        mock_parsing_base_class.return_value = mock_parsing_base

        parser = LogBatcher(self.config_path)
        parser.buffer_max_size = 3
        parser.flush_timeout = 0.05  # 50 ms timeout

        logs = [
            {"id": "1", "message": "test log 1"},
            {"id": "2", "message": "test log 2"},
            {"id": "3", "message": "test log 3"},
            {"id": "4", "message": "test log 4"}
        ]

        # Ensure SimilarityCluster doesn't route everything to noise for simple test behavior
        parser.cluster_type = "LengthCluster"

        # The run should parse and flush when limits are met
        results = parser.parse(logs)
        self.assertEqual(len(results), 4)

    @patch('core.logbatcher.parser.OllamaClient')
    @patch('core.logbatcher.parser.get_sampler')
    @patch('core.logbatcher.parser.ParsingBase')
    def test_noise_quarantine(self, mock_parsing_base_class, mock_get_sampler, mock_ollama_client_class):
        # Mock LLM components
        mock_client = MagicMock()
        mock_ollama_client_class.return_value = mock_client
        mock_sampler = MagicMock()
        mock_get_sampler.return_value = mock_sampler
        mock_parsing_base = MagicMock()
        mock_parsing_base_class.return_value = mock_parsing_base

        parser = LogBatcher(self.config_path)
        parser.cluster_type = "SimilarityCluster"
        parser.similarity_threshold = 0.99
        parser.buffer_max_size = 500

        logs = [
            {"id": "1", "message": "hello world"},
            {"id": "2", "message": "different message entirely"}
        ]

        # Since threshold is 0.99 and min_samples=2, DBSCAN treats them as noise (label -1)
        results = parser.parse(logs)

        # Output results should not contain them (they bypass LLM and Reconciliation)
        self.assertEqual(len(results), 0)

        # They should be written to the quarantine.jsonl file
        self.assertTrue(os.path.exists(self.quarantine_file))
        with open(self.quarantine_file, 'r') as qf:
            lines = [json.loads(line.strip()) for line in qf]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["message"], "hello world")
        self.assertEqual(lines[1]["message"], "different message entirely")

    def test_cache_setter(self):
        cache = ParsingCache(max_size=3)
        entries = [
            {"template": "t2", "ref_log": "raw2", "frequency": 10},
            {"template": "t1", "ref_log": "raw1", "frequency": 5}
        ]
        cache.cache = entries

        # Verify items exist and correct fields mapped
        self.assertEqual(len(cache.cache), 2)
        # Stored order in self.cache: most recent first (t2 was first in JSON, so it is MRU)
        self.assertEqual(cache.cache[0].template, "t2")
        self.assertEqual(cache.cache[0].ref_log, "raw2")
        self.assertEqual(cache.cache[0].frequency, 10)

        self.assertEqual(cache.cache[1].template, "t1")
        self.assertEqual(cache.cache[1].ref_log, "raw1")
        self.assertEqual(cache.cache[1].frequency, 5)

    def test_tfidf_cosine_clustering(self):
        logs = [
            {"message": "hello world"},
            {"message": "hello world"},
            {"message": "hello python"}
        ]
        clusterer = SimilarityCluster(logs, threshold=0.8, vectorizer_type="tfidf")
        partitions = clusterer.get_partitions()

        self.assertIsNotNone(clusterer.dist_matrix)
        self.assertAlmostEqual(clusterer.dist_matrix[0, 1], 0.0)

    def test_similar_sampler(self):
        from component_3_unified_parser.core.logbatcher.sample import SimilarSampler
        sampler = SimilarSampler(batch_size=2)
        logs = [
            {"message": "hello world"},
            {"message": "hello world"},
            {"message": "different log"}
        ]
        sampled = sampler.sample(logs)
        self.assertEqual(len(sampled), 2)
        self.assertEqual(sampled[0]["message"], "hello world")
        self.assertEqual(sampled[1]["message"], "hello world")
