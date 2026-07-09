"""Parsing Cache manager for LogBatcher with LRU Eviction support."""

from collections import OrderedDict

class ParsingCacheEntry:
    """Wrapper entry that supports dict-like access to stay compatible with matching.py."""

    def __init__(self, cache, template, ref_log, frequency):
        self._cache = cache
        self.template = template
        self.ref_log = ref_log
        self.frequency = frequency

    def __getitem__(self, key):
        if key == "template":
            return self.template
        elif key == "ref_log":
            return self.ref_log
        elif key == "frequency":
            return self.frequency
        raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def __setitem__(self, key, value):
        if key == "frequency":
            self.frequency = value
            if self.template in self._cache._data:
                self._cache._data[self.template]["frequency"] = value
                self._cache._data.move_to_end(self.template)
        else:
            raise KeyError(key)

class ParsingCache:
    """Frequency and recency managed template cache using OrderedDict for LRU eviction."""

    def __init__(self, max_size=5000):
        """Initializes the ParsingCache storage with a max size limit."""
        self._data = OrderedDict()
        self.max_size = max_size

    def add(self, template, ref_log):
        """Adds a template to the cache, updating its LRU position or evicting if full."""
        if template in self._data:
            self._data[template]["frequency"] += 1
            self._data.move_to_end(template)
        else:
            if len(self._data) >= self.max_size:
                # Evict the oldest (Least Recently Used) template: first item in OrderedDict
                self._data.popitem(last=False)
            self._data[template] = {
                "ref_log": ref_log,
                "frequency": 1
            }

    @property
    def cache(self):
        """Returns a list of dict-like entries in Most Recently Used (MRU) order."""
        return [
            ParsingCacheEntry(self, k, v["ref_log"], v["frequency"])
            for k, v in reversed(self._data.items())
        ]

    @cache.setter
    def cache(self, entries):
        """Deserializes and loads cache entries, preserving frequency and LRU order."""
        self._data.clear()
        if not entries:
            return
        # Load in reverse of the stored MRU order so that the most recent items end up at the end of the OrderedDict.
        for entry in reversed(entries):
            template = entry.get("template")
            if template:
                self._data[template] = {
                    "ref_log": entry.get("ref_log", ""),
                    "frequency": entry.get("frequency", 1)
                }

    def sort_cache(self):
        """Dummy sort method for compatibility; order is maintained by OrderedDict."""
        pass
