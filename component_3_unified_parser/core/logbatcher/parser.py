"""LogBatcher pipeline orchestrator.

Implements zero-shot diverse parsing routing using partition clusterers,
diversity samplers (DPP), and post-process match and prune logic.
"""

import re
import time
import yaml
import json
import os
import signal
import logging
import collections
from core.llm_client import OllamaClient
from .cluster import get_clusterer
from .parsing_cache import ParsingCache
from .original_cache import OriginalParsingCache
from .matching import match_log, template_to_regex, RegexTimeoutException, regex_timeout_handler
from .sample import get_sampler
from .parsing_base import ParsingBase
from .postprocess import match_and_prune, apply_original_postprocessing, clean_template

logger = logging.getLogger(__name__)

def _guarded_cluster_match(pattern, local_cluster, timeout=1):
    """Matches pattern against every log in local_cluster under a per-log
    SIGALRM timeout, same mitigation already used by matching.py::match_log()
    and original_cache.py::safe_search(). Root-caused via a live botsv3 hang
    (2026-07-21): a BOTSv3 Zeek/SSL log's LLM-generated template captured a
    21-element cipher-list array element-by-element, producing 95 separate
    <*> placeholders; matching that regex against a cluster log whose shape
    didn't align exactly forced catastrophic backtracking with no bound,
    freezing the single-threaded pipeline indefinitely (reproduced offline:
    still running after 10s). template_to_regex() now also collapses
    delimiter-joined <*> runs (not just whitespace-joined ones) to keep this
    rare in practice; this timeout is the backstop for whatever that doesn't
    catch. Matches within the timeout are kept; a log that times out is
    simply treated as unmatched.
    """
    matched = []
    old_handler = signal.signal(signal.SIGALRM, regex_timeout_handler)
    try:
        for log in local_cluster:
            signal.alarm(timeout)
            try:
                if pattern.match(log['message']):
                    matched.append(log)
            except RegexTimeoutException:
                pass
            finally:
                signal.alarm(0)
    finally:
        signal.signal(signal.SIGALRM, old_handler)
    return matched

# Regex patterns for pre-masking obvious variables in noise logs (no LLM needed)
VARIABLE_PATTERNS = [
    r'\b\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b',
    r'\b0[xX][a-fA-F0-9]+\b',
    r'\b[a-fA-F0-9]{8}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{4}-[a-fA-F0-9]{12}\b',
    r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
    r'\b\d+(?:\.\d+)?\b',
]

def _regex_premask(message):
    """Replaces obvious variable tokens with <*> using regex patterns.

    Provides a cheap template approximation for noise logs without LLM cost.

    Args:
        message (str): Raw log message.

    Returns:
        str: Pre-masked template string.
    """
    masked = message
    for pattern in VARIABLE_PATTERNS:
        masked = re.sub(pattern, '<*>', masked)
    # Collapse consecutive <*> tokens separated by spaces
    masked = re.sub(r'<\*>\s*(?:<\*>\s*)+', '<*> ', masked).strip()
    return masked

def format_duration(seconds):
    """Formats a duration in seconds into a human-readable HH:MM:SS or MM:SS format.

    Duplicated from main_parser.py's helper of the same name to avoid a circular
    import (main_parser.py conditionally imports this module).

    Args:
        seconds (float): Duration in seconds.

    Returns:
        str: Formatted duration string.
    """
    if seconds is None or seconds < 0:
        return "0s"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    if h > 0:
        return f"{h}h {m}m {s:.1f}s"
    elif m > 0:
        return f"{m}m {s:.1f}s"
    else:
        return f"{s:.1f}s"

def jaccard_similarity(tokens1, tokens2):
    """Calculates Jaccard similarity between two token lists."""
    set1 = set(tokens1)
    set2 = set(tokens2)
    if not set1 and not set2:
        return 1.0
    union_len = len(set1.union(set2))
    if union_len == 0:
        return 0.0
    return len(set1.intersection(set2)) / union_len

def not_varibility(logs):
    """Faithful port of upstream LogBatcher's util.py::not_varibility().

    True if a batch of log messages is identical once digits are stripped --
    i.e. no real diversity besides numbers, so batch_truncation_mode="original"
    truncates it rather than spending LLM budget on it.
    """
    stripped = [re.sub(r'\d+', '', log) for log in logs]
    return len(set(stripped)) == 1

class LogBatcher:
    """Zero-shot diverse log parser using mathematical sampling and caching."""

    def __init__(self, config_path='/app/config.yaml'):
        """Initializes the LogBatcher parser.

        Args:
            config_path (str): YAML file config path. Defaults to '/app/config.yaml'.
        """
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
        except Exception:
            config = {}

        lb_config = config.get('logbatcher', {})
        self.batch_size = lb_config.get('batch_size', 10)
        self.cluster_type = lb_config.get('cluster', 'LengthCluster')
        self.sampler_type = lb_config.get('sampler', 'DPPSampler')
        self.similarity_threshold = lb_config.get('cluster_similarity_threshold', 0.8)
        self.vectorizer_type = lb_config.get('vectorizer', 'binary')
        self.use_dynamic_eps = lb_config.get('use_dynamic_eps', False)

        # Buffer parameters
        self.buffer_max_size = lb_config.get('buffer_max_size', 500)
        self.flush_timeout = lb_config.get('flush_timeout_seconds', 5.0)

        self.embedding_length_threshold = lb_config.get('embedding_length_threshold', 4000)

        self.llm_client = OllamaClient(config_path)
        self.llm_client.embedding_model = lb_config.get('embedding_model', 'nomic-embed-text')
        if self.embedding_length_threshold is not None:
            # Keep the two in sync when a real threshold is set. If threshold is null
            # (DPP length-routing disabled), leave embedding_char_limit at its own
            # default — get_embedding()'s truncation still needs a numeric cap.
            self.llm_client.embedding_char_limit = self.embedding_length_threshold

        # cache_mode: "production" (default) is our own hardened LRU +
        # Jaccard-reconciliation + regex-verify cache. "original" is a
        # faithful port of upstream LogBatcher's prefix-tree + hash-exact-
        # match cache with per-log matching -- see original_cache.py and
        # parser_implementation_comparison.md Section 1.
        self.cache_mode = lb_config.get('cache_mode', 'production')
        if self.cache_mode == 'original':
            self.cache = OriginalParsingCache()
        else:
            self.cache = ParsingCache(max_size=5000)  # Limit cache to 5000 entries

        # postprocess_mode: "production" (default) keeps clean_template()'s
        # markdown-stripping only. "original" additionally runs upstream's
        # full correct_single_template() normalization cascade + the
        # verify_template() degenerate-output gate on fresh LLM output.
        self.postprocess_mode = lb_config.get('postprocess_mode', 'production')

        # prompt_mode: "production" (default) is our worked-example prompt
        # with direct <*> output. "original" is a faithful port of upstream's
        # zero-shot, 15-type-taxonomy prompt with historical-variables
        # grounding. Independent of postprocess_mode -- see parsing_base.py.
        self.prompt_mode = lb_config.get('prompt_mode', 'production')
        self._variable_candidates = []  # upstream's variable_candidates equivalent; only grows/used under prompt_mode="original"
        # Disclosed adaptation, not upstream fidelity: upstream includes the
        # *entire* unbounded variable_candidates list in every prompt with no
        # cap. Benchmarked directly against real logs: LLM latency scales
        # ~linearly with list size (1.0s @ 0 vars -> 15.2s @ 1000 vars), so
        # since the list only grows over a run, cumulative time grows
        # roughly quadratically with call count -- fine at the paper's
        # original benchmark scale, not at a full-loghub (1.17M log) scale.
        # None (default) = uncapped, exact upstream fidelity.
        self.historical_variables_cap = lb_config.get('historical_variables_cap', None)

        # batch_truncation_mode: "production" (default) sends the sampler's
        # full output to the LLM. "original" truncates a digit-only-variance
        # batch to 3 logs first (upstream's not_varibility() check).
        self.batch_truncation_mode = lb_config.get('batch_truncation_mode', 'production')

        similar_sampler_mode = lb_config.get('similar_sampler_mode', 'production')
        dpp_kernel_mode = lb_config.get('dpp_kernel_mode', 'production')
        self.sampler = get_sampler(
            self.sampler_type, self.llm_client, self.batch_size,
            embedding_length_threshold=self.embedding_length_threshold,
            similar_sampler_mode=similar_sampler_mode,
            dpp_kernel_mode=dpp_kernel_mode
        )
        self.parsing_base = ParsingBase(self.llm_client)

        # Noise log re-queue state
        self.noise_max_retries = lb_config.get('noise_max_retries', 2)
        self._noise_retry_counts = {}  # log_id → retry count
        self._noise_buffer = []        # noise logs awaiting re-queue into next micro-batch

        # noise_mode: "production" (default) leaves DBSCAN noise for
        # _handle_noise_logs()'s 3-tier fallback below. "original" reassigns
        # noise into new clusters inside SimilarityCluster itself (faithful
        # port of upstream's reassign_clusters()) -- see additional_cluster.py
        # and parser_implementation_comparison.md Section 1. LengthCluster
        # ignores this (no noise concept).
        self.noise_mode = lb_config.get('noise_mode', 'production')

    def parse(self, log_list, time_limit=None, start_time=None):
        """Parses a list of logs using a hybrid buffer DBSCAN clustering architecture.

        Args:
            log_list (list): List of dicts representing logs (must contain 'id' and 'message').
            time_limit (float, optional): Maximum execution duration in seconds.
            start_time (float, optional): Parser execution start timestamp.

        Returns:
            list: List of parsed log dictionaries containing mapped templates.
        """
        if start_time is None:
            start_time = time.perf_counter()

        parsed_results = {}
        history = []
        counters = {
            'log_volume': 0,
            'llm_invocations': 0,
            'cache_hits': 0
        }
        total_logs = len(log_list)
        self._last_progress_log_time = start_time

        quarantine_path = 'data/parsed/quarantine.jsonl'
        os.makedirs(os.path.dirname(quarantine_path), exist_ok=True)

        buffer = collections.deque()
        oldest_log_time = None

        for log in log_list:
            buffer.append(log)
            if oldest_log_time is None:
                oldest_log_time = time.perf_counter()

            # Flush triggers:
            # 1. Volume: buffer size reaches max size
            # 2. Time: timeout has passed since oldest log entered
            current_time = time.perf_counter()
            if len(buffer) >= self.buffer_max_size or (current_time - oldest_log_time) >= self.flush_timeout:
                micro_batch = list(buffer)
                buffer.clear()
                oldest_log_time = None
                self._process_micro_batch(
                    micro_batch, parsed_results, quarantine_path,
                    start_time, time_limit, history, counters, total_logs
                )

        if buffer:
            micro_batch = list(buffer)
            buffer.clear()
            self._process_micro_batch(
                micro_batch, parsed_results, quarantine_path,
                start_time, time_limit, history, counters, total_logs
            )

        # Drain any noise logs re-queued out of the final micro-batch. There is no
        # subsequent batch left for them to defer to, so resolve them now (cache
        # match, else regex pre-mask) instead of silently dropping them.
        if self._noise_buffer:
            leftover_noise = self._noise_buffer
            self._noise_buffer = []
            self._handle_noise_logs(leftover_noise, parsed_results, quarantine_path, counters, allow_requeue=False)

        self.history = history

        results = []
        for log in log_list:
            if log['id'] in parsed_results:
                results.append({
                    'id': log['id'],
                    'message': log['message'],
                    'template': parsed_results[log['id']]
                })
        return results

    def _handle_noise_logs(self, noise_logs, parsed_results, quarantine_path, counters, allow_requeue=True):
        """Routes DBSCAN noise logs through cache match -> re-queue -> regex pre-mask.

        Args:
            noise_logs (list): Logs labeled as DBSCAN outliers (-1) for the current pass.
            parsed_results (dict): Shared id -> template map, updated in place.
            quarantine_path (str): Destination file for Tier 3 (regex-masked) logs.
            counters (dict): Shared log_volume/llm_invocations/cache_hits counters.
            allow_requeue (bool): If False, skips Tier 2 (re-queue) and sends
                unmatched logs straight to Tier 3. Used for the final drain pass,
                where there is no subsequent micro-batch left to re-queue into.
        """
        if not noise_logs:
            return

        quarantine_written = []
        cache_matched_count = 0
        requeued_count = 0
        for log in noise_logs:
            msg = log.get('message', '')
            log_id = log['id']

            # Tier 1: Cache match (free)
            if self.cache_mode == 'original':
                # OriginalParsingCache has no .cache list (production-only
                # API) -- use its own match_event() instead.
                template, _, _ = self.cache.match_event(msg)
                cached_template = template if template != "NoMatch" else None
            else:
                cached_template = match_log(self.cache, msg)
            if cached_template:
                parsed_results[log_id] = cached_template
                counters['cache_hits'] += 1
                counters['log_volume'] += 1
                cache_matched_count += 1
                continue

            # Tier 2: Re-queue into next micro-batch (max retries)
            if allow_requeue:
                retries = self._noise_retry_counts.get(log_id, 0)
                if retries < self.noise_max_retries:
                    self._noise_retry_counts[log_id] = retries + 1
                    self._noise_buffer.append(log)
                    requeued_count += 1
                    continue

            # Tier 3: Regex pre-masking (cheap, no LLM)
            masked_template = _regex_premask(msg)
            parsed_results[log_id] = masked_template
            counters['log_volume'] += 1
            quarantine_written.append(log)

            # Clean up retry tracking for this log
            self._noise_retry_counts.pop(log_id, None)

        logger.info(f"Noise handling: {len(noise_logs)} noise logs → "
                   f"{cache_matched_count} cache-matched, "
                   f"{requeued_count} re-queued, "
                   f"{len(quarantine_written)} regex-masked")

        # Write Tier 3 logs to quarantine file for audit
        if quarantine_written:
            try:
                with open(quarantine_path, 'a', encoding='utf-8') as qf:
                    for log in quarantine_written:
                        qf.write(json.dumps(log) + '\n')
            except Exception as e:
                logger.error(f"Error writing to quarantine.jsonl: {e}")

    def _process_micro_batch(self, micro_batch, parsed_results, quarantine_path, start_time, time_limit, history, counters, total_logs=0):
        current_time = time.perf_counter()
        elapsed = current_time - start_time
        if time_limit and elapsed > time_limit:
            logger.warning("Time limit exceeded. Skipping micro-batch.")
            return

        # 0. Prepend any noise logs buffered from the previous micro-batch
        if self._noise_buffer:
            micro_batch = self._noise_buffer + micro_batch
            self._noise_buffer = []

        # 1. Cluster the micro-batch
        clusterer = get_clusterer(
            self.cluster_type,
            micro_batch,
            self.similarity_threshold,
            self.vectorizer_type,
            self.use_dynamic_eps,
            noise_mode=self.noise_mode
        )
        local_clusters = clusterer.get_partitions()

        # 2. 3-Tier Noise Log Handling
        # Outlier logs (DBSCAN label -1) go through: cache match → re-queue → regex pre-mask
        noise_logs = getattr(clusterer, 'noise_logs', [])
        self._handle_noise_logs(noise_logs, parsed_results, quarantine_path, counters)

        # 3. Process each local cluster in queue
        queue = list(local_clusters)

        while queue:
            current_time = time.perf_counter()
            elapsed = current_time - start_time
            if time_limit and elapsed > time_limit:
                logger.warning("Time limit reached inside micro-batch processing.")
                break

            if current_time - self._last_progress_log_time >= 10.0:
                self._last_progress_log_time = current_time
                pct = (counters['log_volume'] / total_logs) * 100 if total_logs > 0 else 0
                rate = counters['log_volume'] / elapsed if elapsed > 0 else 0
                limit_str = f" | Time Left: {format_duration(time_limit - elapsed)}" if time_limit else ""
                logger.info(
                    f"Progress (LogBatcher): parsed {counters['log_volume']}/{total_logs} ({pct:.2f}%) | "
                    f"Speed: {rate:.1f} logs/s | Cache Hits: {counters['cache_hits']} | "
                    f"LLM Calls: {counters['llm_invocations']}{limit_str}"
                )

            local_cluster = queue.pop(0)
            if not local_cluster:
                continue

            # Extract Medoid
            if hasattr(clusterer, 'get_medoid'):
                medoid_log = clusterer.get_medoid(local_cluster)
            else:
                medoid_log = local_cluster[0]

            if self.cache_mode == 'original':
                # Faithful port of upstream's per-log cache.match_event() loop
                # (see original_cache.py / parser_implementation_comparison.md
                # Section 1) instead of our own medoid-level Jaccard
                # reconciliation below.
                self._process_local_cluster_original(
                    local_cluster, medoid_log, parsed_results, counters, history,
                    queue, time_limit, start_time
                )
                continue

            # 4. Global Reconciliation against cache medoids
            best_match = None
            best_similarity = -1.0
            medoid_tokens = medoid_log['message'].split()

            for entry in self.cache.cache:
                ref_tokens = entry['ref_log'].split()
                sim = jaccard_similarity(medoid_tokens, ref_tokens)
                if sim > best_similarity:
                    best_similarity = sim
                    best_match = entry

            if best_match and best_similarity >= self.similarity_threshold:
                # Cache Hit: Route logs through cached template
                cached_template = best_match['template']
                best_match['frequency'] += 1
                self.cache.sort_cache()

                matched, pruned = match_and_prune(cached_template, local_cluster, self.cache)

                if matched:
                    for log in matched:
                        parsed_results[log['id']] = cached_template

                    counters['log_volume'] += len(matched)
                    counters['cache_hits'] += len(matched)
                    history.append({
                        'log_volume': counters['log_volume'],
                        'llm_invocations': counters['llm_invocations'],
                        'cache_hits': counters['cache_hits']
                    })

                    if pruned:
                        queue.append(pruned)
                else:
                    # False-positive cache hit: reconciliation matched by lexical
                    # (Jaccard) similarity against the medoid, but the regex-verified
                    # match found nothing. Requeuing local_cluster unchanged here would
                    # reconcile against the same unchanged cache and fail identically
                    # forever -- nothing about the cache or cluster would ever change
                    # between attempts. Fall through to the cache-miss path instead,
                    # which guarantees forward progress on this cluster.
                    self._process_cache_miss(
                        local_cluster, medoid_log, parsed_results, counters, history,
                        queue, time_limit, start_time
                    )
            else:
                self._process_cache_miss(
                    local_cluster, medoid_log, parsed_results, counters, history,
                    queue, time_limit, start_time
                )

    def _process_cache_miss(self, local_cluster, medoid_log, parsed_results, counters, history, queue, time_limit, start_time):
        """Samples, queries the LLM for a new template, and records the result.

        Used both for a genuine cache miss (no cached template cleared the
        reconciliation threshold) and for a false-positive cache hit (a
        reconciled template matched by Jaccard similarity but verified zero
        matches via regex) -- in both cases, a fresh LLM query is the only way
        to make forward progress on this cluster.
        """
        sampled = self.sampler.sample(local_cluster, time_limit=time_limit, start_time=start_time)
        if self.batch_truncation_mode == 'original':
            sampled = self._maybe_truncate_batch(sampled)
        generated_template = self.parsing_base.batch_query(
            sampled, prompt_mode=self.prompt_mode, historical_variables=self._get_historical_variables()
        )
        if generated_template and self.postprocess_mode == 'original':
            generated_template = apply_original_postprocessing(generated_template, medoid_log['message'])
        if generated_template:
            # Always clean before any downstream use (matching, storage,
            # variable extraction) -- prompt_mode="original" asks for
            # backtick-delimited output, so the raw string can still carry
            # backticks/markdown at this point even after postprocess_mode.
            # match_and_prune() below does its own internal clean_template()
            # for matching, but previously used the *uncleaned* string for
            # parsed_results/cache.add, silently dormant under
            # prompt_mode="production" (whose own prompt forbids markdown)
            # but a real bug once prompt_mode="original" is in play.
            generated_template = clean_template(generated_template)

        if generated_template:
            matched, pruned = match_and_prune(generated_template, local_cluster, self.cache)
            if len(matched) > 0:
                for log in matched:
                    parsed_results[log['id']] = generated_template

                # Medoid message becomes reference log
                self.cache.add(generated_template, medoid_log['message'])
                if self.prompt_mode == 'original':
                    self._update_variable_candidates(medoid_log['message'], generated_template)

                counters['log_volume'] += len(matched)
                counters['llm_invocations'] += 1
                history.append({
                    'log_volume': counters['log_volume'],
                    'llm_invocations': counters['llm_invocations'],
                    'cache_hits': counters['cache_hits']
                })

                if pruned:
                    queue.append(pruned)
            else:
                logger.warning("Generated template failed to match any logs in local cluster. Falling back to raw messages.")
                for log in local_cluster:
                    parsed_results[log['id']] = log['message']

                counters['log_volume'] += len(local_cluster)
                counters['llm_invocations'] += 1
                history.append({
                    'log_volume': counters['log_volume'],
                    'llm_invocations': counters['llm_invocations'],
                    'cache_hits': counters['cache_hits']
                })
        else:
            for log in local_cluster:
                parsed_results[log['id']] = log['message']

            counters['log_volume'] += len(local_cluster)
            history.append({
                'log_volume': counters['log_volume'],
                'llm_invocations': counters['llm_invocations'],
                'cache_hits': counters['cache_hits']
            })

    def _process_local_cluster_original(self, local_cluster, medoid_log, parsed_results, counters, history, queue, time_limit, start_time):
        """cache_mode == 'original': per-log matching against OriginalParsingCache,
        mirroring upstream's get_responce() loop (logbatcher/parser.py) instead of
        this repo's medoid-level Jaccard reconciliation.

        Checks each log in the cluster against the cache in turn; the first log
        with a cache hit determines the template used to split the cluster into
        matched (recorded now) and unmatched (requeued), via template_to_regex --
        same anchored-regex semantics as upstream's prune_from_cluster. If no log
        gets a cache hit, or the hit's regex-verified match is empty (the same
        lexical-vs-structural disagreement risk fixed for the production path in
        _process_micro_batch), falls through to a fresh LLM query on the whole
        cluster, guaranteeing forward progress.
        """
        matched_template = None
        for log in local_cluster:
            template, _, _ = self.cache.match_event(log['message'])
            if template != "NoMatch":
                matched_template = template
                break

        if matched_template is not None:
            try:
                pattern = template_to_regex(matched_template)
                matched = _guarded_cluster_match(pattern, local_cluster)
            except Exception:
                matched = []

            if matched:
                matched_ids = {log['id'] for log in matched}
                unmatched = [log for log in local_cluster if log['id'] not in matched_ids]

                for log in matched:
                    parsed_results[log['id']] = matched_template

                counters['log_volume'] += len(matched)
                counters['cache_hits'] += len(matched)
                history.append({
                    'log_volume': counters['log_volume'],
                    'llm_invocations': counters['llm_invocations'],
                    'cache_hits': counters['cache_hits']
                })

                if unmatched:
                    queue.append(unmatched)
                return

        # No log in the cluster got a cache hit, or the hit didn't survive
        # regex verification -- fresh LLM query on the whole cluster.
        sampled = self.sampler.sample(local_cluster, time_limit=time_limit, start_time=start_time)
        if self.batch_truncation_mode == 'original':
            sampled = self._maybe_truncate_batch(sampled)
        generated_template = self.parsing_base.batch_query(
            sampled, prompt_mode=self.prompt_mode, historical_variables=self._get_historical_variables()
        )
        if generated_template and self.postprocess_mode == 'original':
            generated_template = apply_original_postprocessing(generated_template, medoid_log['message'])
        if generated_template:
            # See _process_cache_miss()'s equivalent comment: this path never
            # ran the LLM output through anything backtick-aware at all, so
            # skipping this would make prompt_mode="original" (which asks for
            # backtick-delimited output) always fail to match here.
            generated_template = clean_template(generated_template)

        if generated_template:
            try:
                pattern = template_to_regex(generated_template)
                matched = _guarded_cluster_match(pattern, local_cluster)
            except Exception:
                matched = []

            if matched:
                matched_ids = {log['id'] for log in matched}
                pruned = [log for log in local_cluster if log['id'] not in matched_ids]

                for log in matched:
                    parsed_results[log['id']] = generated_template

                # Matches upstream's real call site (parsing_base.py): insert=False
                # with relevant_templates left at its default [] -- the LCS-merge
                # branch is unreachable there and here alike, by design; see
                # parser_implementation_comparison.md Section 1.
                self.cache.add_templates(
                    event_template=generated_template, insert=False,
                    refer_log=medoid_log['message']
                )
                if self.prompt_mode == 'original':
                    self._update_variable_candidates(medoid_log['message'], generated_template)

                counters['log_volume'] += len(matched)
                counters['llm_invocations'] += 1
                history.append({
                    'log_volume': counters['log_volume'],
                    'llm_invocations': counters['llm_invocations'],
                    'cache_hits': counters['cache_hits']
                })

                if pruned:
                    queue.append(pruned)
            else:
                logger.warning("Generated template failed to match any logs in local cluster. Falling back to raw messages.")
                for log in local_cluster:
                    parsed_results[log['id']] = log['message']

                counters['log_volume'] += len(local_cluster)
                counters['llm_invocations'] += 1
                history.append({
                    'log_volume': counters['log_volume'],
                    'llm_invocations': counters['llm_invocations'],
                    'cache_hits': counters['cache_hits']
                })
        else:
            for log in local_cluster:
                parsed_results[log['id']] = log['message']

            counters['log_volume'] += len(local_cluster)
            history.append({
                'log_volume': counters['log_volume'],
                'llm_invocations': counters['llm_invocations'],
                'cache_hits': counters['cache_hits']
            })

    def _maybe_truncate_batch(self, sampled):
        """batch_truncation_mode == 'original': faithful port of upstream's
        Cluster.batching() min_size truncation -- if the sampled batch is
        identical once digits are stripped (not_varibility()), there's no
        real diversity to show the LLM beyond numbers, so truncate to 3
        (upstream's min_size default) instead of spending full batch budget.
        """
        if len(sampled) <= 3:
            return sampled
        messages = [log.get('message', '') for log in sampled]
        if not_varibility(messages):
            return sampled[:3]
        return sampled

    def _get_historical_variables(self):
        """Returns the historical_variables to pass into the next prompt --
        the full list if historical_variables_cap is None (uncapped, upstream
        fidelity), otherwise the most recent N entries. Called fresh at each
        cache-miss LLM query, not cached, since self._variable_candidates
        keeps growing throughout the run."""
        if self.historical_variables_cap is None:
            return self._variable_candidates
        return self._variable_candidates[-self.historical_variables_cap:]

    def _update_variable_candidates(self, refer_log, template):
        """prompt_mode == 'original': faithful port of vars.py::vars_update(),
        reusing template_to_regex() instead of a duplicate extract_variables()
        implementation -- its capture groups already give the matched
        variable values directly. Grows self._variable_candidates, fed back
        into future prompts as historical-variables grounding.

        Upstream's own vars_update() does `var not in candidates` against its
        full unbounded candidates list -- an O(n) scan repeated on every
        successful LLM call. At full-dataset scale this list grows into the
        thousands and the scan cost compounds into the same quadratic
        slowdown historical_variables_cap was meant to fix (that cap only
        bounded the slice sent to the prompt, not this membership check or
        the list's own growth). When historical_variables_cap is set, trim
        the underlying list itself to match, keeping both the scan and the
        prompt payload bounded.
        """
        old_handler = signal.signal(signal.SIGALRM, regex_timeout_handler)
        signal.alarm(1)
        try:
            match = template_to_regex(template).match(refer_log)
            values = match.groups() if match else ()
        except Exception:
            values = ()
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)
        for val in values:
            val = re.sub(r'^\((.*)\)$|^\[(.*)\]$', r'\1\2', val)
            if (val and val not in self._variable_candidates
                    and not val.isdigit() and not val.isalpha()
                    and len(val.split()) <= 3):
                self._variable_candidates.append(val)
        if self.historical_variables_cap is not None and len(self._variable_candidates) > self.historical_variables_cap:
            self._variable_candidates = self._variable_candidates[-self.historical_variables_cap:]
