import time
import logging
import sys
import os

# Add paths so Python can find core modules
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)
sys.path.append(os.path.join(parent_dir, 'component_3_unified_parser'))

from core.llm_client import OllamaClient
from core.logbatcher.parsing_base import ParsingBase

logging.basicConfig(level=logging.INFO)

def run_test():
    # Define a realistic batch of 5 logs (e.g. OpenSSH failed logins)
    batch_logs = [
        {"message": "Failed password for invalid user admin from 192.168.1.100 port 54321 ssh2"},
        {"message": "Failed password for invalid user guest from 10.0.0.5 port 39281 ssh2"},
        {"message": "Failed password for invalid user backup from 172.16.50.4 port 48102 ssh2"},
        {"message": "Failed password for invalid user user1 from 8.8.8.8 port 59321 ssh2"},
        {"message": "Failed password for invalid user temp_user from 4.2.2.2 port 60231 ssh2"}
    ]

    print("Initializing OllamaClient and ParsingBase...")
    client = OllamaClient('/app/config.yaml')
    print(f"Configured Model Name: {client.model_name}")
    print(f"Configured API URL: {client.base_url}")
    
    pb = ParsingBase(client)

    prompt = (
        "Here is a batch of diverse logs from the same system. They share the same static template "
        "but contain different dynamic variables. Identify the static template they share by replacing "
        "the varying parameters with the placeholder <*>. Output ONLY the final template string.\n\n"
    )
    for i, log in enumerate(batch_logs):
        prompt += f"Log {i+1}: {log.get('message', '')}\n"

    print("\n==================================================")
    print("FORMATTED PROMPT:")
    print("==================================================")
    print(prompt)
    print("==================================================")

    print("\nSending batch query request to Ollama...")
    start_time = time.perf_counter()
    result = pb.batch_query(batch_logs)
    elapsed = time.perf_counter() - start_time

    print("\n==================================================")
    print("RESPONSE METRICS:")
    print("==================================================")
    print(f"Extracted Template: '{result}'")
    print(f"Duration:           {elapsed:.4f} seconds")
    print(f"LLM Usage Info:     {client.get_usage()}")
    print("==================================================\n")

if __name__ == '__main__':
    run_test()
