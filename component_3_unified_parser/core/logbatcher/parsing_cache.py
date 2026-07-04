"""Parsing Cache manager for LogBatcher.

Implements sorting by query frequency and mapping cached templates to reference logs.
"""

class ParsingCache:
    """Frequency-sorted in-memory template lookup cache."""

    def __init__(self):
        """Initializes the ParsingCache storage."""
        self.cache = []

    def add(self, template, ref_log):
        """Adds a template to the cache or increments its frequency count if it exists.

        Args:
            template (str): Normalized template content.
            ref_log (str): Reference raw log message.
        """
        for entry in self.cache:
            if entry["template"] == template:
                entry["frequency"] += 1
                self.sort_cache()
                return
        self.cache.append({
            "template": template,
            "ref_log": ref_log,
            "frequency": 1
        })
        self.sort_cache()

    def sort_cache(self):
        """Sorts the cache items in descending frequency order."""
        self.cache.sort(key=lambda x: x["frequency"], reverse=True)
