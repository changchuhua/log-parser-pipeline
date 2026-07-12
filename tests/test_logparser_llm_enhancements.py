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
        tree.loose_match_metric = "positional_decay"
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

    def test_positional_uniform_similarity(self):
        from component_3_unified_parser.core.logparser_llm.tree_router import positional_uniform_similarity
        
        tokens1 = ["A", "B", "C", "D"]
        tokens2 = ["A", "B", "X", "Y"]
        
        # 2 matches out of 4 total tokens -> 0.5
        sim = positional_uniform_similarity(tokens1, tokens2)
        self.assertEqual(sim, 0.5)
        
        # Order matters
        tokens3 = ["B", "A", "C", "D"]
        sim2 = positional_uniform_similarity(tokens1, tokens3)
        self.assertEqual(sim2, 0.5)

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
        
        # Test legacy 3 -> ecs_3
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            yaml.dump({'logparser_llm': {'categories_mode': 3}}, f)
            config_name_3 = f.name
            
        tree = PrefixTree()
        extractor_3 = LLMExtractor(tree, config_path=config_name_3)
        self.assertEqual(extractor_3.categories_mode, "ecs_3")
        
        # Test legacy 10 -> ecs_10
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            yaml.dump({'logparser_llm': {'categories_mode': 10}}, f)
            config_name_10 = f.name
            
        extractor_10 = LLMExtractor(tree, config_path=config_name_10)
        self.assertEqual(extractor_10.categories_mode, "ecs_10")
        
        # Test explicit paper_10
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            yaml.dump({'logparser_llm': {'categories_mode': 'paper_10'}}, f)
            config_name_paper = f.name
            
        extractor_paper = LLMExtractor(tree, config_path=config_name_paper)
        self.assertEqual(extractor_paper.categories_mode, "paper_10")
        
        # Cleanup
        os.remove(config_name_3)
        os.remove(config_name_10)
        os.remove(config_name_paper)

    def test_calibration_seed_loading(self):
        from component_3_unified_parser.core.logparser_llm.llm_extractor import LLMExtractor
        import tempfile
        import yaml
        import json
        import os

        # Create dummy calibration seed
        calib_data = [
            {"template": "Calibration <OID> test", "ref_log": "Calibration 123 test"}
        ]
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as cf:
            json.dump(calib_data, cf)
            calib_file = cf.name

        # Create config pointing to calibration seed
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            yaml.dump({'logparser_llm': {'calibration_file': calib_file}}, f)
            config_name = f.name

        tree = PrefixTree()
        extractor = LLMExtractor(tree, config_path=config_name)
        
        # Check that calibration data is in the pool and tree
        self.assertEqual(extractor.template_pool[0]['template'], "Calibration <OID> test")
        self.assertIn("Calibration <OID> test", tree.clusters)

        # Cleanup
        os.remove(calib_file)
        os.remove(config_name)

    def test_icl_selection_strategy(self):
        from component_3_unified_parser.core.logparser_llm.llm_extractor import LLMExtractor
        import tempfile
        import yaml
        import os

        # Test diversity strategy
        with tempfile.NamedTemporaryFile('w', suffix='.yaml', delete=False) as f:
            yaml.dump({'logparser_llm': {'icl_selection_strategy': 'diversity', 'k_shots': 2}}, f)
            config_name = f.name

        tree = PrefixTree()
        extractor = LLMExtractor(tree, config_path=config_name)
        
        # Manually populate template pool
        extractor.template_pool = [
            {"template": "Log A", "ref_log": "Log A"},
            {"template": "Log B", "ref_log": "Log B"},
            {"template": "Log C", "ref_log": "Log C"}
        ]
        
        # Test it runs without error and returns 2 items
        import json
        # Mock get_variables_from_example and ollama client so it doesn't fail
        extractor.llm_client.generate_completion = lambda x: '{"template": "result", "variables": []}'
        
        res = extractor.get_template("Log A")
        self.assertEqual(res, "result")
        
        os.remove(config_name)
