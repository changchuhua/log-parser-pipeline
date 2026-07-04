import yaml
import json
import csv
import logging
from core.llm_client import OllamaClient
from core.logbatcher.cluster import LogClusterer
from core.logbatcher.parsing_cache import ParsingCache
from core.logbatcher.sample import DiversitySampler
from core.logbatcher.parsing_base import ZeroShotPrompter
from core.logbatcher.postprocess import PostProcessor

logger = logging.getLogger(__name__)

def load_config(config_path='/app/config.yaml'):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

class LogBatcherMethod:
    def __init__(self):
        config = load_config()
        self.batch_size = config.get('logbatcher', {}).get('batch_size', 10)
        self.threshold = config.get('logbatcher', {}).get('cluster_similarity_threshold', 0.8)
        
        self.llm_client = OllamaClient()
        self.llm_client.embedding_model = config.get('logbatcher', {}).get('embedding_model', 'nomic-embed-text')
        
        self.clusterer = LogClusterer(threshold=self.threshold)
        self.cache = ParsingCache()
        self.sampler = DiversitySampler(self.llm_client, batch_size=self.batch_size)
        self.prompter = ZeroShotPrompter(self.llm_client)
        self.postprocessor = PostProcessor()
        
    def run(self, input_files, output_csv):
        logger.info("Starting LogBatcher Pipeline...")
        
        logs = []
        line_id = 1
        for in_file in input_files:
            with open(in_file, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        msg = record.get('message', '')
                        if msg:
                            logs.append({
                                'id': record.get('event', {}).get('id', line_id),
                                'message': msg,
                                'tokens': msg.split(' ')
                            })
                            line_id += 1
                    except:
                        pass
                        
        logger.info(f"Loaded {len(logs)} logs. Initial clustering...")
        clusters = self.clusterer.initial_partition(logs)
        logger.info(f"Created {len(clusters)} initial partitions.")
        
        parsed_results = []
        
        queue = clusters
        while queue:
            current_cluster = queue.pop(0)
            if not current_cluster:
                continue
                
            first_log = current_cluster[0]
            cached_template = self.cache.match(first_log['message'], first_log['tokens'])
            
            if cached_template:
                matched, pruned = self.postprocessor.match_and_prune(cached_template, current_cluster, self.cache)
                for log in matched:
                    parsed_results.append((log['id'], log['message'], cached_template))
                if pruned:
                    queue.append(pruned)
            else:
                sampled = self.sampler.sample(current_cluster)
                raw_template = self.prompter.generate_template(sampled)
                template = self.postprocessor.clean_template(raw_template)
                
                if template:
                    matched, pruned = self.postprocessor.match_and_prune(template, current_cluster, self.cache)
                    for log in matched:
                        parsed_results.append((log['id'], log['message'], template))
                    if pruned:
                        queue.append(pruned)
                else:
                    log = current_cluster[0]
                    parsed_results.append((log['id'], log['message'], log['message']))
                    if len(current_cluster) > 1:
                        queue.append(current_cluster[1:])
                        
        logger.info(f"Parsing complete. Writing to {output_csv}...")
        with open(output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['LineId', 'Content', 'EventTemplate'])
            for row in parsed_results:
                writer.writerow(row)
        logger.info("LogBatcher Output Saved.")
