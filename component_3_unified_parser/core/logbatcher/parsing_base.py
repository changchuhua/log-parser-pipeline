class ZeroShotPrompter:
    def __init__(self, llm_client):
        self.llm_client = llm_client
        
    def generate_template(self, batch_logs):
        prompt = (
            "Here is a batch of diverse logs from the same system. They share the same static template "
            "but contain different dynamic variables. Identify the static template they share by replacing "
            "the varying parameters with the placeholder <*>. Output ONLY the final template string.\n\n"
        )
        
        for i, log in enumerate(batch_logs):
            prompt += f"Log {i+1}: {log['message']}\n"
            
        try:
            template = self.llm_client.generate_completion(prompt).strip()
            return template
        except Exception as e:
            print(f"[!] Prompt generation error: {e}")
            return None
