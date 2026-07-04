"""Pre-processing static regex manager for LibreLog.

Pre-compiles common pattern signatures (IPs, Hex, UUIDs) to mask dynamic variables
before querying LLM endpoints.
"""

import re

class RegexManager:
    """Manages static compiled regex patterns for cleaning log strings."""

    def __init__(self):
        """Pre-compiles regex rules targeting IPs, Hex numbers, and UUIDs."""
        self.patterns = [
            (re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'), '<*>'), 
            (re.compile(r'\b0x[a-fA-F0-9]+\b'), '<*>'),                     
            (re.compile(r'\b[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\b'), '<*>'), 
        ]

    def mask(self, text):
        """Applies pre-compiled regex filters to replace variables with placeholders.

        Args:
            text (str): Raw input log message.

        Returns:
            str: Masked log message.
        """
        masked_text = text
        for pattern, replacement in self.patterns:
            masked_text = pattern.sub(replacement, masked_text)
        return masked_text
