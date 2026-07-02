import numpy as np
from sklearn.metrics.pairwise import cosine_similarity
import scipy.linalg

def dpp_sample(kernel_matrix, k):
    n = kernel_matrix.shape[0]
    if k >= n:
        return list(range(n))
        
    selected = []
    diag = np.diag(kernel_matrix)
    selected.append(np.argmax(diag))
    
    while len(selected) < k:
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
            
    return selected

class DiversitySampler:
    def __init__(self, llm_client, batch_size=10):
        self.llm_client = llm_client
        self.batch_size = batch_size
        
    def sample(self, cluster_logs):
        if len(cluster_logs) <= self.batch_size:
            return cluster_logs
            
        embeddings = []
        for log in cluster_logs:
            emb = self.llm_client.get_embedding(log['message'])
            embeddings.append(emb)
            
        emb_matrix = np.array(embeddings)
        kernel_matrix = cosine_similarity(emb_matrix)
        
        selected_indices = dpp_sample(kernel_matrix, self.batch_size)
        return [cluster_logs[i] for i in selected_indices]
