import time
import unittest
from unittest.mock import MagicMock, patch
from component_3_unified_parser.core.logparser_llm.tree_router import PrefixTree, Node
from component_3_unified_parser.core.logparser_llm.llm_extractor import LLMExtractor, get_jaccard_similarity, get_variables_from_example

class TestLogParserLLMEnhancements(unittest.TestCase):
    def test_jaccard_similarity(self):
        self.assertAlmostEqual(get_jaccard_similarity("hello world", "hello world"), 1.0)
        self.assertAlmostEqual(get_jaccard_similarity("hello world", "hello python"), 1.0 / 3.0)

    def test_get_variables_from_example(self):
        template = "User <LOI> logged in"
        ref_log = "User admin logged in"
        vars = get_variables_from_example(template, ref_log)
        self.assertEqual(len(vars), 1)
        self.assertEqual(vars[0]["category"], "<LOI>")
        self.assertEqual(vars[0]["value"], "admin")

    def test_tree_router_recency_and_pruning(self):
        tree = PrefixTree()
        # Insert a template
        tree.insert("User <LOI> logged in")
        # Find leaf node
        current = tree.root
        for token in "User <LOI> logged in".split(' '):
            current = current.children[token]

        self.assertIsNotNone(current.last_matched)
        initial_time = current.last_matched

        # Mock match updating
        time.sleep(0.01)
        # strict match should update it
        tree.strict_match("User <LOI> logged in".split(' '))
        self.assertGreater(current.last_matched, initial_time)

        # prune test:
        # If we prune with max_age_seconds = 1 and offset current_time by 10s, it should delete the template and prune the tree branch!
        tree.prune_inactive_templates(current_time=time.time() + 10, max_age_seconds=1)
        self.assertEqual(len(tree.clusters), 0)
        self.assertIsNone(current.cluster)
        # Since it is pruned, the tree children under 'User' should be cleared recursively
        self.assertNotIn("User", tree.root.children)

    @patch('component_3_unified_parser.core.logparser_llm.llm_extractor.OllamaClient')
    def test_llm_extractor_json_and_ecs_mapping(self, mock_ollama_client_class):
        mock_client = MagicMock()
        mock_client.generate_completion.return_value = '{"template": "User <LOI> logged in", "variables": [{"category": "<LOI>", "value": "1.2.3.4"}]}'
        mock_ollama_client_class.return_value = mock_client

        tree = PrefixTree()
        extractor = LLMExtractor(tree)

        record = {}
        template = extractor.get_template("User 1.2.3.4 logged in", record)

        self.assertEqual(template, "User <LOI> logged in")
        # ECS field "source.ip" should be added to record
        self.assertEqual(record.get("source.ip"), "1.2.3.4")

    def test_weighted_jaccard_positional(self):
        from component_3_unified_parser.core.logparser_llm.tree_router import weighted_jaccard_similarity
        
        # Mismatch at index 0 (weight = 1.0)
        tokens1 = ["ERROR", "connection", "from", "ip", "closed"]
        tokens2 = ["INFO", "connection", "from", "ip", "closed"]
        sim_mismatch_first = weighted_jaccard_similarity(tokens1, tokens2, decay_factor=0.15)
        
        # Mismatch at index 4 (weight = e^(-0.15*4) = 0.548)
        tokens3 = ["ERROR", "connection", "from", "ip", "closed"]
        tokens4 = ["ERROR", "connection", "from", "ip", "open"]
        sim_mismatch_last = weighted_jaccard_similarity(tokens3, tokens4, decay_factor=0.15)
        
        # Mismatch at the end should yield higher similarity score than at the start
        self.assertGreater(sim_mismatch_last, sim_mismatch_first)
        
        # Verify PrefixTree loose match integration
        tree = PrefixTree()
        tree.use_positional_weighting = True
        tree.decay_factor = 0.15
        tree.loose_match_threshold = 0.7
        tree.clusters.append("ERROR connection from <*> closed")
        
        # Starting token mismatch fails Jaccard loose match
        self.assertIsNone(tree.loose_match(["INFO", "connection", "from", "ip", "closed"]))
        
        # Trailing token mismatch passes Jaccard loose match
        self.assertEqual(
            tree.loose_match(["ERROR", "connection", "from", "ip", "open"]),
            "ERROR connection from <*> closed"
        )

    def test_wildcard_node_merging(self):
        from component_3_unified_parser.core.logparser_llm.template_manager import TemplateManager
        tree = PrefixTree()
        tree.clusters = [
            "User admin logged in",
            "User system logged in"
        ]
        # Insert them into tree
        for t in tree.clusters:
            tree.insert(t)
            
        manager = TemplateManager(tree)
        # Similarity threshold 0.75 (3 out of 4 tokens match)
        manager.merge_similarity_threshold = 0.75
        
        manager.calibrate()
        
        # They should be merged into "User <*> logged in"
        self.assertEqual(len(tree.clusters), 1)
        self.assertEqual(tree.clusters[0], "User <*> logged in")
        
        # Verify strict match against the newly merged tree works
        self.assertEqual(tree.strict_match(["User", "guest", "logged", "in"]), "User <*> logged in")

    def test_capacity_pruning(self):
        tree = PrefixTree()
        tree.insert("t1")
        # Match t1 to set its matched timestamp
        tree.strict_match(["t1"])
        
        time.sleep(0.01)
        tree.insert("t2")
        tree.strict_match(["t2"])
        
        time.sleep(0.01)
        tree.insert("t3")
        tree.strict_match(["t3"])
        
        # Capping at max_templates = 2
        # It should evict "t1" because it has the oldest timestamp
        tree.prune_to_capacity(max_templates=2)
        
        self.assertEqual(len(tree.clusters), 2)
        self.assertNotIn("t1", tree.clusters)
        self.assertIn("t2", tree.clusters)
        self.assertIn("t3", tree.clusters)

    def test_categories_mode_toggle(self):
        from component_3_unified_parser.core.logparser_llm.llm_extractor import LLMExtractor
        import tempfile
        import yaml
        import os
        
        # Test categories_mode = 3
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            yaml.dump({'logparser_llm': {'categories_mode': 3}}, f)
            config_name_3 = f.name
            
        tree = PrefixTree()
        extractor_3 = LLMExtractor(tree, config_path=config_name_3)
        self.assertEqual(extractor_3.categories_mode, 3)
        
        # Test categories_mode = 10
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            yaml.dump({'logparser_llm': {'categories_mode': 10}}, f)
            config_name_10 = f.name
            
        extractor_10 = LLMExtractor(tree, config_path=config_name_10)
        self.assertEqual(extractor_10.categories_mode, 10)
        
        # Cleanup
        os.remove(config_name_3)
        os.remove(config_name_10)
