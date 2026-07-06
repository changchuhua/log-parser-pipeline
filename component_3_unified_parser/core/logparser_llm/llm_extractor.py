"""In-Context Learning (ICL) Template Extractor for LogParser-LLM.

Computes log embeddings, extracts similar examples dynamically from a seed pool,
and queries the Ollama client to extract templates with semantic categories.
"""

import numpy as np
import logging
from sklearn.metrics.pairwise import cosine_similarity
from core.llm_client import OllamaClient
import yaml

logger = logging.getLogger(__name__)

class LLMExtractor:
    """Handles parsing logs using dynamic few-shot templates querying the LLM."""

    def __init__(self, tree_router, config_path='/app/config.yaml'):
        """Initializes LLMExtractor.

        Args:
            tree_router (PrefixTree): In-memory PrefixTree router references.
            config_path (str): YAML configuration path. Defaults to '/app/config.yaml'.
        """
        self.tree_router = tree_router
        self.llm_client = OllamaClient(config_path)
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}
        self.k_shots = config.get('logparser_llm', {}).get('k_shots', 3)
        self.seed_pool = [] # list of dicts: {'log': str, 'template': str, 'embedding': np.array}
        
    def get_template(self, log_message):
        """Calculates embeddings and queries Ollama to extract the template.

        Uses dynamic K-shot prompt generation based on cosine similarity of log embeddings.

        Args:
            log_message (str): Raw log message content.

        Returns:
            str: Evaluated static template.
        """
        # Calculate embedding
        try:
            emb = self.llm_client.get_embedding(log_message)
            emb_array = np.array(emb).reshape(1, -1)
        except Exception as e:
            logger.error(f"Embedding error: {e}")
            return log_message # fallback to literal if error
            
        # Dynamic K-Shot (ICL)
        demonstrations = ""
        if self.seed_pool:
            pool_embs = np.vstack([x['embedding'] for x in self.seed_pool])
            sims = cosine_similarity(emb_array, pool_embs)[0]
            top_indices = sims.argsort()[-self.k_shots:][::-1]
            for idx in top_indices:
                demonstrations += f"Log: {self.seed_pool[idx]['log']}\nTemplate: {self.seed_pool[idx]['template']}\n\n"
                
        # The System Prompt
        sys_prompt = (
            "As a log parser, your task is to analyze logs and identify dynamic variables. "
            "The categories are: Object ID (<OID>), Location Indicator (<LOC>), Object Name (<OBJ>), "
            "Type Indicator (<TYP>), Switch Indicator (<SWI>), Time/Duration (<TIM>), "
            "Computing Resources (<RES>), Object Amount (<AMT>), Status Code (<STA>), and Other Parameters (<OTH>). "
            "Substitute dynamic variables with their respective category tokens. "
            "Everything outside the tokens should remain exactly unchanged. Do not fix typos. "
            "If a variable is a compound string, replace the entire compound with a single token.\n\n"
        )
        
        if demonstrations:
            sys_prompt += f"Examples:\n{demonstrations}\n"
            
        prompt = sys_prompt + f"Log: {log_message}\nTemplate:"
        
        try:
            template = self.llm_client.generate_completion(prompt).strip()
            # Update Tree
            self.tree_router.insert(template)
            # Add to seed pool
            self.seed_pool.append({'log': log_message, 'template': template, 'embedding': emb_array[0]})
            return template
        except Exception as e:
            logger.error(f"LLM Generation error: {e}")
            return log_message
