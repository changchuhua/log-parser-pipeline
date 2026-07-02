import re

class ParsingCache:
    def __init__(self):
        self.cache = []
        
    def _sort_cache(self):
        self.cache.sort(key=lambda x: x['frequency'], reverse=True)
        
    def add_template(self, template, ref_log):
        for item in self.cache:
            if item['template'] == template:
                item['frequency'] += 1
                self._sort_cache()
                return
                
        escaped = re.escape(template)
        regex_str = escaped.replace(r'\<\*\>', r'(.*?)')
        regex_str = f"^{regex_str}$"
        
        try:
            pattern = re.compile(regex_str)
            token_len = len(ref_log.split(' '))
            self.cache.append({
                'template': template,
                'ref_log': ref_log,
                'frequency': 1,
                'regex': pattern,
                'token_len': token_len
            })
            self._sort_cache()
        except Exception as e:
            pass
            
    def match(self, log_message, log_tokens):
        for item in self.cache:
            if len(log_tokens) == item['token_len']:
                if item['regex'].match(log_message):
                    item['frequency'] += 1
                    self._sort_cache()
                    return item['template']
        return None
