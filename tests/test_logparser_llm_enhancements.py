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
