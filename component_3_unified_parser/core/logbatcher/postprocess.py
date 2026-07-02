import re

class PostProcessor:
    def clean_template(self, llm_output):
        if not llm_output:
            return ""
        template = llm_output.strip()
        if template.startswith("```"):
            template = template.split('\n', 1)[-1]
            if template.endswith("```"):
                template = template.rsplit('\n', 1)[0]
        template = template.replace('`', '').strip()
        return template

    def match_and_prune(self, template, cluster_logs, cache):
        escaped = re.escape(template)
        regex_str = f"^{escaped.replace(r'\\<\\*\\>', r'(.*?)')}$"
        
        matched_logs = []
        pruned_logs = []
        
        try:
            pattern = re.compile(regex_str)
        except:
            return matched_logs, cluster_logs
            
        for log in cluster_logs:
            if pattern.match(log['message']):
                matched_logs.append(log)
            else:
                pruned_logs.append(log)
                
        if matched_logs:
            cache.add_template(template, matched_logs[0]['message'])
            
        return matched_logs, pruned_logs
