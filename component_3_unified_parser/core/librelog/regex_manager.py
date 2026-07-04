import re

class RegexManager:
    def __init__(self):
        self.patterns = [
            (re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'), '<*>'), 
            (re.compile(r'\b0x[a-fA-F0-9]+\b'), '<*>'),                     
            (re.compile(r'\b[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\b'), '<*>'), 
        ]

    def mask(self, text):
        masked_text = text
        for pattern, replacement in self.patterns:
            masked_text = pattern.sub(replacement, masked_text)
        return masked_text
