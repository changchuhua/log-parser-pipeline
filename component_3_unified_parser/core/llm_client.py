import requests
import yaml

class OllamaClient:
    def __init__(self, config_path='/app/config.yaml'):
        with open(config_path, 'r') as f:
            config = yaml.safe_load(f)
            
        self.base_url = config.get('llm', {}).get('base_url', 'http://host.docker.internal:11434/v1')
        self.model_name = config.get('llm', {}).get('model_name', 'llama3')
        self.embedding_model = config.get('logparser_llm', {}).get('embedding_model', 'nomic-embed-text')
        
    def get_embedding(self, text):
        url = f"{self.base_url}/embeddings"
        payload = {
            "model": self.embedding_model,
            "input": text
        }
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data['data'][0]['embedding']
        
    def generate_completion(self, prompt, temperature=0.0):
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature
        }
        resp = requests.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        return data['choices'][0]['message']['content']
