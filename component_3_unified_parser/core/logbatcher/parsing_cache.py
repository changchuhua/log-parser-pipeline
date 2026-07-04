class ParsingCache:
    def __init__(self):
        self.cache = []

    def add(self, template, ref_log):
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
        self.cache.sort(key=lambda x: x["frequency"], reverse=True)
