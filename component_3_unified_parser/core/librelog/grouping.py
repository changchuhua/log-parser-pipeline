"""Log grouping manager for LibreLog.

Partitions logs based on token length and prefix tokens to optimize similarity searches.
"""

class GroupingManager:
    """Groups logs by their structural footprint (token length and prefix)."""

    def __init__(self):
        """Initializes the GroupingManager mapping store."""
        self.groups = {}

    def get_group_key(self, log_message):
        """Computes a hashable partitioning key for a log message.

        The key is composed of (token_length, prefix_tuple).

        Args:
            log_message (str): Masked log message content.

        Returns:
            tuple: (length int, prefix tuple).
        """
        tokens = log_message.split()
        length = len(tokens)
        prefix = tuple(tokens[:min(3, length)])
        return (length, prefix)

    def add_to_group(self, log_message, log_id):
        """Maps a log ID to its designated structural group.

        Args:
            log_message (str): Masked log message content.
            log_id (str): Log message unique identifier.

        Returns:
            tuple: Computed group key.
        """
        key = self.get_group_key(log_message)
        if key not in self.groups:
            self.groups[key] = []
        self.groups[key].append(log_id)
        return key

    def get_group_logs(self, log_message):
        """Retrieves list of log IDs belonging to the same structural group.

        Args:
            log_message (str): Masked reference log message.

        Returns:
            list: List of mapped log IDs.
        """
        key = self.get_group_key(log_message)
        return self.groups.get(key, [])
