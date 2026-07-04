import random
import numpy as np
import logging
from sklearn.metrics.pairwise import cosine_similarity

logger = logging.getLogger(__name__)

class Sampler:
    def __init__(self, batch_size=10):
        self.batch_size = batch_size

    def sample(self, logs):
        raise NotImplementedError()

class RandomSampler(Sampler):
    def sample(self, logs):
        if len(logs) <= self.batch_size:
            return logs
        return random.sample(logs, self.batch_size)

class DPPSampler(Sampler):
    def __init__(self, llm_client, batch_size=10):
        super().__init__(batch_size)
        self.llm_client = llm_client

    def sample(self, logs):
        if len(logs) <= self.batch_size:
            return logs

        embeddings = []
        for log in logs:
            msg = log.get('message', '')
            try:
                emb = self.llm_client.get_embedding(msg)
                embeddings.append(emb)
            except Exception as e:
                logger.error(f"Failed to get embedding: {e}")
                embeddings.append(np.random.rand(768).tolist())

        emb_matrix = np.array(embeddings)
        kernel_matrix = cosine_similarity(emb_matrix)

        n = kernel_matrix.shape[0]
        selected = []
        diag = np.diag(kernel_matrix)
        selected.append(np.argmax(diag))

        while len(selected) < self.batch_size:
            unselected = [i for i in range(n) if i not in selected]
            max_vol = -1
            best_i = -1

            for i in unselected:
                candidate_idx = selected + [i]
                submatrix = kernel_matrix[np.ix_(candidate_idx, candidate_idx)]
                vol = np.linalg.det(submatrix)
                if vol > max_vol:
                    max_vol = vol
                    best_i = i

            if best_i != -1:
                selected.append(best_i)
            else:
                break

        return [logs[i] for i in selected]

def get_sampler(sampler_type, llm_client, batch_size=10):
    if sampler_type == "DPPSampler":
        return DPPSampler(llm_client, batch_size)
    elif sampler_type == "RandomSampler":
        return RandomSampler(batch_size)
    else:
        raise ValueError(f"Unknown sampler type: {sampler_type}")
