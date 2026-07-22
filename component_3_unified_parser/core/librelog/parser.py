"""LibreLog pipeline orchestrator.

Implements regex preprocessing, semantic clustering, dynamic example memory lookup,
and LLM-based template parsing with self-reflection.
"""

import time
import logging
import os
import yaml
from core.llm_client import OllamaClient
from .grouping import LogParser as DrainParser
from .regex_manager import RegexTemplateManager
from .llama_parser import LogParser as LlamaParser

logger = logging.getLogger(__name__)

GLOBAL_VARIABLE_RULES = [
    # 1. ISO/System Timestamps
    r"\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b",
    # 2. Hexadecimal Addresses / Memory Pointers
    r"\b0[xX][a-fA-F0-9]+\b",
    # 3. UUIDs
    r"\b[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\b",
    # 4. Standard IPv4 / IPv6 Addresses
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b",
    # 5. Generic Numbers (Integers and Floating Points)
    r"\b\d+(?:\.\d+)?\b"
]

DATASET_REGEXES = {
    "HDFS": [r"blk_-?\d+", r"(\d+\.){3}\d+(:\d+)?"],
    "Hadoop": [r"(\d+\.){3}\d+"],
    "Spark": [r"(\d+\.){3}\d+", r"\b[KGTM]?B\b", r"([\w-]+\.){2,}[\w-]+"],
    "Zookeeper": [r"(/|)(\d+\.){3}\d+(:\d+)?"],
    "BGL": [r"core\.\d+"],
    "HPC": [r"=\d+"],
    "Thunderbird": [r"(\d+\.){3}\d+"],
    "Windows": [r"0x.*?\s"],
    "Linux": [r"(\d+\.){3}\d+", r"\d{2}:\d{2}:\d{2}"],
    "Android": [r"(/[\w-]+)+", r"([\w-]+\.){2,}[\w-]+", r"\b(\-?\+?\d+)\b|\b0[Xx][a-fA-F\d]+\b|\b[a-fA-F\d]{4,}\b"],
    "HealthApp": [],
    "Apache": [r"(\d+\.){3}\d+"],
    "Proxifier": [r"<\d+\ssec", r"([\w-]+\.)+[\w-]+(:\d+)?", r"\d{2}:\d{2}(:\d{2})*", r"[KGTM]B"],
    "OpenSSH": [r"(\d+\.){3}\d+", r"([\w-]+\.){2,}[\w-]+"],
    "OpenStack": [r"((\d+\.){3}\d+,?)+", r"/.+?\s", r"\d+"],
    "Mac": [r"([\w-]+\.){2,}[\w-]+"]
}

DATASET_SETTINGS = {
    "HDFS": {"st": 0.5, "depth": 4},
    "Hadoop": {"st": 0.5, "depth": 4},
    "Spark": {"st": 0.5, "depth": 4},
    "Zookeeper": {"st": 0.5, "depth": 4},
    "BGL": {"st": 0.5, "depth": 4},
    "HPC": {"st": 0.5, "depth": 4},
    "Thunderbird": {"st": 0.5, "depth": 4},
    "Windows": {"st": 0.7, "depth": 5},
    "Linux": {"st": 0.39, "depth": 6},
    "Android": {"st": 0.2, "depth": 6},
    "HealthApp": {"st": 0.2, "depth": 4},
    "Apache": {"st": 0.5, "depth": 4},
    "Proxifier": {"st": 0.6, "depth": 3},
    "OpenSSH": {"st": 0.6, "depth": 5},
    "OpenStack": {"st": 0.5, "depth": 5},
    "Mac": {"st": 0.7, "depth": 6},
    # "default" = the bucket name used for datasets without per-line "Prefix_N"
    # LineIds (e.g. botsv3/Security Onion logs — see main_parser.py's dataset
    # inference). These are heavily structured (JSON/CSV-style) logs where the
    # 5 GLOBAL_VARIABLE_RULES regex patterns mask out most tokens before Drain
    # ever sees them (e.g. AWS VPC Flow Log lines are ~12/14 tokens numeric).
    # seqDist() doesn't award similarity credit for matching wildcard positions,
    # so the achievable similarity ceiling for such logs is far below 0.5 even
    # when two logs share the exact same template — empirically ~0.2-0.3 is the
    # actual ceiling, so st=0.5 makes every log fail to match and fastMatch's
    # per-log bucket scan grows unbounded (confirmed via profiling: 0% match
    # rate, O(n^2) blowup). st=0.2 was verified against a 20k-log botsv3 sample
    # to cluster correctly (20 distinct, sensible templates) at ~1s vs. >45s timeout.
    "default": {"st": 0.2, "depth": 4}
}

class DummyMemory:
    """Memory store mimicking a real memory database for warm cache starts.

    Unbounded by default (max_size=None). If max_size is set, evicts the
    oldest entry (FIFO) once exceeded.
    """
    def __init__(self, max_size=None):
        self.memory = []
        self.max_size = max_size

    def add(self, entry):
        """Appends an entry, evicting the oldest one if over capacity."""
        if self.max_size is not None and len(self.memory) >= self.max_size:
            self.memory.pop(0)
        self.memory.append(entry)

class LibreLogParser:
    """Unified parser router implementation of LibreLog framework."""

    def __init__(self, dataset_name='default', config_path='/app/config.yaml'):
        """Initializes the LibreLogParser.

        Args:
            dataset_name (str): The dataset name to load settings for.
            config_path (str): YAML file config path. Defaults to '/app/config.yaml'.
        """
        self.dataset_name = dataset_name

        # Load config.yaml up front so it can inform Drain settings, LlamaParser
        # construction, and memory bounds below.
        if not os.path.exists(config_path) and config_path == '/app/config.yaml':
            config_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), 'config.yaml')
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception as e:
            logger.warning(f"Could not load config from {config_path}: {e}")
            config = {}

        librelog_config = config.get('librelog', {})

        # `similarity_threshold` becomes the fallback Drain `st` for datasets not
        # covered by DATASET_SETTINGS' per-dataset tuned values.
        default_st = librelog_config.get('similarity_threshold', 0.5)
        settings = DATASET_SETTINGS.get(dataset_name, {"st": default_st, "depth": 4})
        self.st = settings["st"]
        self.depth = settings["depth"]
        # GLOBAL_VARIABLE_RULES exists to give unlisted/custom datasets (e.g.
        # botsv3) *some* preprocessing, since they have no dedicated regex list
        # of their own. It must NOT apply to the paper's own 16 tuned datasets --
        # upstream's real call site (evaluator.py) only ever passes that dataset's
        # own regex list, no global prepending. Applying a blanket generic-number
        # mask (one of the 5 global rules) ahead of Drain changes what literal
        # tokens Drain's own similarity/generalization logic sees, for datasets
        # the paper explicitly tuned assuming that preprocessing wasn't there.
        if dataset_name in DATASET_REGEXES:
            self.rex = DATASET_REGEXES[dataset_name]
        else:
            self.rex = GLOBAL_VARIABLE_RULES + DATASET_REGEXES.get(dataset_name, [])

        self.llm_client = OllamaClient(config_path)
        self.regex_manager = RegexTemplateManager()
        k_shots = librelog_config.get('k_shots', 3)
        enable_reflection = librelog_config.get('enable_reflection', True)
        self.llama_parser = LlamaParser(
            llm_client=self.llm_client,
            regex_manager1=self.regex_manager,
            model=self.llm_client.model_name,
            regex_sample=k_shots,
            similarity="jaccard",
            do_self_reflection=str(bool(enable_reflection))
        )
        max_memory_size = librelog_config.get('max_memory_size', None)
        self.memory = DummyMemory(max_size=max_memory_size)

        # memory_mode: "production" (default) adds an exact-raw-string, cluster-level
        # cache_map in front of the paper's own mechanism, backed by DummyMemory so it
        # persists across separate --use-cache/--write-cache invocations. "original" is
        # the paper's actual Template Memory design (Section III.C): templates stored as
        # regex patterns, sorted by token count, binary-searched -- which is
        # RegexTemplateManager, and it already runs unconditionally inside
        # LlamaParser.parse() via find_matched_regex_template(), self-contained (every
        # verified template gets registered via add_regex_template()). The paper doesn't
        # describe cross-process persistence -- its evaluation is a single continuous
        # run -- so "original" mode disables cache_map entirely and relies solely on
        # RegexTemplateManager, which starts empty each run regardless of --use-cache.
        self.memory_mode = librelog_config.get('memory_mode', 'production')

        self.use_drain_backup = librelog_config.get('use_drain_backup', False)
        self.write_drain_backup = librelog_config.get('write_drain_backup', False)
        cache_dir = config.get('directories', {}).get('cache_dir', 'data/cache')
        self.drain_backup_file = os.path.join(cache_dir, f'librelog_drain_backup_{self.dataset_name}.json')

    def parse(self, logs_to_parse, time_limit=None, start_time=None):
        """Parses list of logs using LibreLog's pipeline logic.

        Args:
            logs_to_parse (list): List of log dictionaries containing message and ID.
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: List of parsed log dictionaries with predicted templates.
        """
        if start_time is None:
            start_time = time.perf_counter()

        # Build in-memory cache lookup table from self.memory.memory. Empty and
        # never populated under memory_mode == "original" -- see __init__ comment.
        cache_map = {}
        if self.memory_mode == 'production':
            cache_map = {entry['raw_log']: entry['template'] for entry in self.memory.memory}

        # Keep a list of log IDs mapped to their messages
        from collections import defaultdict
        message_to_ids = defaultdict(list)
        for log in logs_to_parse:
            message_to_ids[log['message']].append(log['id'])

        raw_logs = [log['message'] for log in logs_to_parse]

        # 1. Drain tree grouping pass
        import json
        grouped_logs = None
        if self.use_drain_backup and os.path.exists(self.drain_backup_file):
            logger.info(f"[{self.dataset_name}] Loading Drain tree grouping from backup file {self.drain_backup_file}...")
            try:
                with open(self.drain_backup_file, 'r', encoding='utf-8') as dbf:
                    grouped_logs = json.load(dbf)
            except Exception as e:
                logger.error(f"[{self.dataset_name}] Error loading Drain backup: {e}")

        if grouped_logs is None:
            logger.info(f"[{self.dataset_name}] Running Drain tree grouping pass on {len(raw_logs)} logs...")
            tree_parser = DrainParser(rex=self.rex, depth=self.depth, st=self.st)
            grouped_logs = tree_parser.parse(raw_logs)
            
            if self.write_drain_backup:
                logger.info(f"[{self.dataset_name}] Writing Drain tree grouping to backup file {self.drain_backup_file}...")
                try:
                    os.makedirs(os.path.dirname(self.drain_backup_file), exist_ok=True)
                    with open(self.drain_backup_file, 'w', encoding='utf-8') as dbf:
                        json.dump(grouped_logs, dbf)
                except Exception as e:
                    logger.error(f"[{self.dataset_name}] Error writing Drain backup: {e}")

        # 2. Partition logs by EventId
        groups_dict = {}
        for item in grouped_logs:
            # item is [Content, EventId, EventTemplate]
            content = item[0]
            evt_id = item[1]
            evt_template = item[2]
            if evt_id not in groups_dict:
                groups_dict[evt_id] = []
            groups_dict[evt_id].append({
                "Content": content,
                "EventId": evt_id,
                "EventTemplate": evt_template
            })

        # 3. Sort groups by content length of the first log message
        def count_words(entry):
            return len(entry["Content"].split())
        sorted_items = sorted(groups_dict.items(), key=lambda x: count_words(x[1][0]))
        groups_dict = {k: v for k, v in sorted_items}

        logger.info(f"[{self.dataset_name}] Grouping completed: {len(groups_dict)} clusters generated.")

        results = []
        history = []
        cache_hits = 0
        parsed_count = 0
        total_logs = len(logs_to_parse)

        # 4. Process each cluster
        for eventid, group_logs in groups_dict.items():
            current_time = time.perf_counter()
            elapsed = current_time - start_time
            if time_limit and elapsed > time_limit:
                logger.warning(f"[{self.dataset_name}] Time limit reached. Stopping early.")
                break

            logs_from_group = [item["Content"] for item in group_logs]
            
            # Check cache hit first (character exact string matching)
            cached_template = None
            for content in logs_from_group:
                if content in cache_map:
                    cached_template = cache_map[content]
                    break

            if cached_template:
                # Cache hit: map cached template to all logs in this group
                cache_hits += len(group_logs)
                for item in group_logs:
                    content = item["Content"]
                    # Pop matching ID
                    if message_to_ids[content]:
                        log_id = message_to_ids[content].pop(0)
                        results.append({
                            'id': log_id,
                            'message': content,
                            'template': cached_template
                        })
                parsed_count += len(group_logs)
                continue

            # Cache miss: run official Llama parser pipeline
            # llama_parser.parse returns res_list: [(Content, EventId/new_event, template), ...]
            try:
                logger.info(f"[{self.dataset_name}] Cluster {eventid}: Cache miss. Invoking LLM parsing phase for {len(group_logs)} logs. Sample: '{logs_from_group[0][:150]}...'")
                res_list = self.llama_parser.parse(group_logs, logs_from_group)
                for content, _, template in res_list:
                    if message_to_ids[content]:
                        log_id = message_to_ids[content].pop(0)
                        results.append({
                            'id': log_id,
                            'message': content,
                            'template': template
                        })
                    if self.memory_mode == 'production':
                        # Add newly generated template back to cache map
                        cache_map[content] = template
                        # Add to memory (bounded FIFO) for cache writing
                        words = content.split()
                        gk_tuple = (len(words), tuple(words[:1]) if words else ())
                        self.memory.add({
                            'raw_log': content,
                            'template': template,
                            'group_key': gk_tuple
                        })
                logger.info(f"[{self.dataset_name}] Cluster {eventid}: LLM parsing phase completed successfully. Generated {len(res_list)} template mappings.")
                parsed_count += len(group_logs)
            except Exception as e:
                logger.error(f"[{self.dataset_name}] LlamaParser error on cluster {eventid}: {e}")
                # Fallback mapping
                for item in group_logs:
                    content = item["Content"]
                    if message_to_ids[content]:
                        log_id = message_to_ids[content].pop(0)
                        results.append({
                            'id': log_id,
                            'message': content,
                            'template': item["EventTemplate"]
                        })
                parsed_count += len(group_logs)

            history.append({
                'log_volume': parsed_count,
                'llm_invocations': self.llm_client.invocations,
                'cache_hits': cache_hits
            })

        self.history = history
        return results
