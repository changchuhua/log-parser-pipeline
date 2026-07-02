import os
import glob
import json
import argparse
import yaml
from tqdm import tqdm
from core.logparser_llm.tree_router import PrefixTree
from core.logparser_llm.llm_extractor import LLMExtractor
from core.logparser_llm.template_manager import TemplateManager
from core.logbatcher_method import LogBatcherMethod

def load_config(config_path='/app/config.yaml'):
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def run_logparser_llm(input_files, output_dir):
    tree_router = PrefixTree()
    llm_extractor = LLMExtractor(tree_router)
    template_manager = TemplateManager(tree_router)
    
    os.makedirs(output_dir, exist_ok=True)
    
    for in_file in input_files:
        base_name = os.path.basename(in_file)
        out_file = os.path.join(output_dir, f"parsed_{base_name}")
        print(f"[*] Processing {in_file} with logparser-llm...")
        
        with open(in_file, 'r', encoding='utf-8') as f_in:
            lines = f_in.readlines()
            
        with open(out_file, 'w', encoding='utf-8') as f_out:
            for line_idx, line in enumerate(tqdm(lines, desc=f"Parsing {base_name}")):
                try:
                    record = json.loads(line.strip())
                    log_message = record.get('message', '')
                    if not log_message:
                        f_out.write(json.dumps(record) + '\n')
                        continue
                        
                    tokens = log_message.split(' ')
                    
                    template = tree_router.strict_match(tokens)
                    if not template:
                        template = tree_router.loose_match(tokens)
                    if not template:
                        template = llm_extractor.get_template(log_message)
                        
                    record['parsed_template'] = template
                    f_out.write(json.dumps(record) + '\n')
                    
                    if (line_idx + 1) % 1000 == 0:
                        template_manager.calibrate()
                        
                except Exception as e:
                    print(f"[!] Error parsing line in {in_file}: {e}")
                    
        template_manager.calibrate()

def main():
    parser = argparse.ArgumentParser(description="Unified Parser")
    parser.add_argument('--method', type=str, required=True, choices=['logparser-llm', 'logbatcher', 'librelog'])
    args = parser.parse_args()
    
    config = load_config()
    input_dir = config.get('directories', {}).get('output_dir', 'data/processed')
    parsed_dir = 'data/parsed'
    os.makedirs(parsed_dir, exist_ok=True)
    
    input_files = glob.glob(os.path.join(input_dir, '*.jsonl'))
    
    if not input_files:
        print(f"[*] No JSONL files found in {input_dir}. Nothing to parse.")
        return
        
    if args.method == 'logparser-llm':
        run_logparser_llm(input_files, parsed_dir)
    elif args.method == 'logbatcher':
        output_csv = os.path.join(parsed_dir, 'logbatcher_output.csv')
        method = LogBatcherMethod()
        method.run(input_files, output_csv)
    elif args.method == 'librelog':
        print("[*] LibreLog method not yet implemented.")

if __name__ == "__main__":
    main()
