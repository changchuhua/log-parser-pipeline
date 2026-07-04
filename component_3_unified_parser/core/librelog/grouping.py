class GroupingManager:
    def __init__(self):
        self.groups = {}

    def get_group_key(self, log_message):
        tokens = log_message.split()
        length = len(tokens)
        prefix = tuple(tokens[:min(3, length)])
        return (length, prefix)

    def add_to_group(self, log_message, log_id):
        key = self.get_group_key(log_message)
        if key not in self.groups:
            self.groups[key] = []
        self.groups[key].append(log_id)
        return key

    def get_group_logs(self, log_message):
        key = self.get_group_key(log_message)
        return self.groups.get(key, [])
