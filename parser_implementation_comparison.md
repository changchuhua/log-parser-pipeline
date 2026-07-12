# Parser Implementation Comparison: Our Repo vs. Reference Sources

> **Methodology:** Every source file in our repo was read line-by-line. Reference implementations were analyzed via GitHub source + paper text. Comparison is objective — no embellishment.

---

## Section 1: LogBatcher

### Reference: [LogIntelligence/LogBatcher](https://github.com/LogIntelligence/LogBatcher)

| Aspect | Original LogBatcher | Our Implementation | Verdict |
|--------|--------------------|--------------------|---------|
| **Clustering** | DBSCAN with TF-IDF vectorization and cosine distance | Two modes: `LengthCluster` (token-length hash, default) and `SimilarityCluster` (DBSCAN with binary Jaccard or TF-IDF cosine) | ✅ **Superset.** Our default (`LengthCluster`) is simpler but we offer DBSCAN as a toggle. Our TF-IDF cosine mode matches the original; binary Jaccard is an addition not in the original. |
| **DBSCAN Parameters** | `eps` derived from similarity threshold, `min_samples=2`, precomputed metric | Identical: `eps = 1.0 - threshold`, `min_samples=2`, `metric='precomputed'`. Added: dynamic eps scaling based on token-length std dev. | ✅ **Faithful + extended** |
| **Sampling (DPP)** | Greedy DPP on embedding kernel matrix. No candidate cap — operates on full cluster. | Greedy DPP identical algorithm (max-determinant selection). **Added 100-candidate pre-sampling cap** to prevent OOM on large clusters. | ✅ **Production-hardened.** The original lacks any safeguard and will OOM on clusters >~500 logs (kernel matrix is O(n²) memory). |
| **DPP Kernel** | Cosine similarity of LLM embeddings | Identical: `cosine_similarity(emb_matrix)` from sklearn | ✅ **Identical** |
| **Additional Samplers** | DPP only | Added `SimilarSampler` (medoid + k-nearest) and `RandomSampler` | ✅ **Superset** |
| **LLM Prompting** | Demonstration-free batch prompt: system instruction defines the LLM as an expert log parser, user message contains a batch of DPP-sampled logs with instructions to extract the static template and replace dynamic variables with `<*>`. No labeled example pairs — the batch itself provides implicit context. | System instruction with user message containing numbered logs. Same `<*>` placeholder convention. | ✅ **Faithful** |
| **Caching** | Simple list with linear scan, frequency counter, no eviction, no size limit | `OrderedDict`-based LRU cache with `max_size=5000`, frequency tracking, MRU-ordered iteration | ✅ **Strictly better.** The original grows unboundedly — a production liability. |
| **Cache Lookup** | Linear scan with Jaccard similarity against `ref_log` tokens | Identical: linear scan, Jaccard similarity, same threshold logic | ⚠️ **Same limitation.** Both are O(n×m). No approximate nearest-neighbor indexing. |
| **Match & Prune** | Template → regex, match against cluster logs, split into matched/pruned | Identical logic. Added: consecutive `<*>` collapsing before regex compilation to prevent catastrophic backtracking. Added: 1-second SIGALRM timeout per regex match. | ✅ **Production-hardened** |
| **Medoid Extraction** | Not clearly documented in original | Mathematical medoid from precomputed distance matrix (minimum sum of distances). Only for `SimilarityCluster`; `LengthCluster` uses first log. | ✅ **Correct implementation** |
| **Noise Handling** | Not addressed | 3-tier fallback for DBSCAN noise (label -1): (1) cache exact match, (2) re-queue for next micro-batch (max 2 retries), (3) regex pre-masking fallback (no LLM). Tier 3 logs still written to quarantine file for audit. | ✅ **Production-hardened.** Solves the problem of dropped noise logs and prevents template explosion without incurring LLM cost. |
| **Micro-Batch Buffering** | Not present — processes all logs in one shot | Stream-oriented buffering with volume trigger (`buffer_max_size=500`) and time trigger (`flush_timeout=5s`) | ✅ **Production addition** for streaming scenarios |
| **Time Budget** | Not present | Time limit checks at micro-batch and per-cluster level, with early termination | ✅ **Production addition** |
| **Persistence** | Not present | Cache serialization/deserialization via JSON, cross-run cache loading (`--use-cache`, `--write-cache`) | ✅ **Production addition** |

### Where the Original is Better

1. **Full-cluster DPP:** The original runs DPP on the entire cluster, which provides theoretically optimal diversity. Our 100-candidate cap trades optimality for safety. For clusters of 100–500 logs, the original's uncapped DPP would select more diverse samples. **Impact: Minor** — the cap is a necessary production trade-off.

2. **No noise loss (Resolved):** The original doesn't quarantine noise, so every log gets a template (even if incorrect). Our implementation previously dropped them, but now uses a 3-tier fallback (cache match → re-queue → regex pre-mask) ensuring every log gets a template without template explosion.

### Production Proxy Assessment

**Rating: Strong proxy with production hardening.**

The core algorithmic pipeline (cluster → sample → LLM → cache → match & prune) is faithfully reproduced. The additions (LRU eviction, candidate capping, time budgets, micro-batching, SIGALRM timeouts) are all necessary for production use and do not alter the fundamental approach. The main gap is that our default clustering mode (`LengthCluster`) is simpler than the paper's DBSCAN — but `SimilarityCluster` is available as a config toggle.

---

## Section 2: LibreLog

### Reference: [zeyang919/LibreLog](https://github.com/zeyang919/librelog) — ICSE 2025

| Aspect | Original LibreLog | Our Implementation | Verdict |
|--------|------------------|--------------------|---------|
| **Grouping Tree** | Drain-based fixed-depth prefix tree: Level 1 = token length, Level 2 = first K prefix tokens, Level 3 = similarity matching | **Identical Drain implementation.** Code in [grouping.py](file:///home/amilame/github/Practicum/log-parser-pipeline/component_3_unified_parser/core/librelog/grouping.py) is ported directly from the LogPAI Drain codebase. Same `treeSearch`, `addSeqToPrefixTree`, `fastMatch`, `seqDist`, `getTemplate` methods. | ✅ **Faithful port** |
| **Drain Parameters** | Per-dataset tuned `st` (similarity threshold) and `depth` values | Identical per-dataset settings in `DATASET_SETTINGS` dict (16 datasets). Values match the paper's Table 2. | ✅ **Faithful** |
| **Regex Preprocessing** | Dataset-specific regex patterns to abstract known variables (IPs, hex, UUIDs, timestamps, numbers) before Drain grouping | Identical: `GLOBAL_VARIABLE_RULES` (5 patterns) + `DATASET_REGEXES` (16 dataset-specific pattern sets). Patterns match the original. | ✅ **Faithful** |
| **LLM Backend** | Meta-Llama-3-8B-Instruct via **HuggingFace `transformers`** (`AutoModelForCausalLM` / `pipeline` API, local GPU inference) | **Ollama** HTTP API (`/api/chat`). Model configurable (default `llama3`, supports `gemma4:26b`, `deepseek-r1:32b`, `qwen3.6:27b`). | ⚠️ **Different backend.** Both are local inference approaches. HuggingFace transformers loads the model directly into GPU memory; Ollama wraps llama.cpp behind an HTTP API. Functionally equivalent with similar single-stream throughput. |
| **LLM Prompting** | 3-block prompt: (1) Task instruction defining template extraction, (2) Fixed few-shot I/O examples, (3) RAG-selected diverse log samples. Output: template with `<*>` wildcards. | Identical 3-block structure in [llama_parser.py](file:///home/amilame/github/Practicum/log-parser-pipeline/component_3_unified_parser/core/librelog/llama_parser.py): system instruction → fixed example pair → user logs. Same `<*>` convention. | ✅ **Faithful** |
| **RAG Sample Selection** | Adaptive Random Sampling: iteratively select the log with maximum Jaccard distance from the already-selected set. k=3 (default). max_logs=200 cap. | **Identical algorithm** in `adaptive_random_sampling()` (L94-125): starts with longest log, iteratively picks most distant by Jaccard/cosine, caps at 200. Supports both Jaccard and cosine distance. | ✅ **Faithful** |
| **Self-Reflection** | After initial LLM template generation, a second LLM query verifies the template against group logs. If logs don't match the regex, re-invoke LLM on the mismatched subset. Up to 3 retry iterations. | **Identical** in `store_regx_for_logs()` (L436-456): `check_regex_from_groups()` validates each log against the generated regex. Wrong logs are re-processed with a new LLM call. Loop runs up to 3 times (`test_time < 3`). | ✅ **Faithful** |
| **Regex Template Manager** | Sorted list of (word_count, regex_template) tuples. Binary search by word count for efficient lookup. Validates regex against a reference log before adding. | **Identical** in [regex_manager.py](file:///home/amilame/github/Practicum/log-parser-pipeline/component_3_unified_parser/core/librelog/regex_manager.py): same `RegexTemplateManager` with `add_regex_template()`, `get_index_by_length()` (binary search), `find_matched_regex_template()`. | ✅ **Faithful port** |
| **Template Memory** | In-paper: sophisticated memory with group-key-aware lookup and Jaccard similarity retrieval | Our `memory.py` implements `LogMemory`. **However**, `parser.py` uses `DummyMemory` backed by a fast O(1) `cache_map` dictionary for exact matches, paired with `RegexManager` for O(log N) regex pattern matching. | ✅ **Production-optimized.** While `LogMemory` (O(N) linear scan similarity matching) is what the paper uses for ICL selection, our `cache_map` + `RegexManager` combination is significantly faster for exact and pattern matches in production, though we lose similarity-based few-shot context for novel logs. |
| **Template Output Format** | Regex patterns with `(.*?)` wildcards | Same: templates are stored as regex patterns. `main_parser.py` converts back to `<*>` format via `regex_to_standard_template()` for output. | ✅ **Faithful** |
| **Dataset Isolation** | Single parser per dataset | Dedicated `LibreLogParser` instance per dataset, preventing cross-dataset cache thrashing | ✅ **Production improvement** |
| **Scalability** | Original has O(N²) complexity in template memory lookup (paper reports linear time via caching) | Our `LogMemory.get_similar_logs()` is O(n) per lookup (linear scan of bounded memory). `regex_manager` uses binary search by word count for faster matching. | ✅ **Matches paper's claimed efficiency** |

### Where the Original is Better

1. **HuggingFace transformers direct inference:** The original loads Meta-Llama-3-8B-Instruct directly via HuggingFace `AutoModelForCausalLM`, bypassing any serving layer overhead. Our Ollama HTTP API adds network serialization latency per call. **Impact: Minor for throughput** — both are single-stream local inference.

2. **Integrated evaluation framework:** The original includes `accuracy.py` and `evaluator.py` for computing GA, PA, FGA, FTA against LogHub-2.0 ground truth. We don't have an equivalent automated evaluation pipeline. **Impact: Moderate** — needed for benchmarking.

3. **Template memory warm-start:** The original can pre-seed memory from previous runs more effectively. Our `DummyMemory` starts cold unless cache is explicitly loaded via CLI flag. **Impact: Moderate.**

### Production Proxy Assessment

**Rating: Faithful proxy with backend trade-off.**

The algorithmic pipeline (regex preprocessing → Drain grouping → adaptive sampling → LLM prompting → self-reflection → regex validation) is reproduced with high fidelity. The Drain tree code is a direct port. The key difference is the LLM backend (Ollama HTTP API vs HuggingFace transformers direct loading), which has minimal throughput impact since both are local single-stream inference. The `DummyMemory` vs `LogMemory` distinction is a deliberate production optimization: our pipeline uses O(1) dict lookups and O(log N) regex matching (`regex_manager`) rather than the paper's O(N) Jaccard similarity linear scans, trading few-shot context for speed.

---

## Section 3: LogParser-LLM

### Reference: [Paper arXiv:2408.13727](https://arxiv.org/abs/2408.13727) — KDD 2024

| Aspect | Paper's LogParser-LLM | Our Implementation | Verdict |
|--------|----------------------|--------------------|---------|
| **Prefix Tree Structure** | Root → token nodes → leaf clusters. Each node stores a token string. Leaf nodes point to cluster objects (template + log ID list). Wildcard `<*>` nodes serve as universal matchers. | Identical: `Node` class with `token`, `children` dict, `cluster` string. Wildcard `<*>` traversal in `strict_match()`. **Added:** `last_matched` timestamp for LRU tracking. | ✅ **Faithful + extended** |
| **Strict Match** | Traverse tree token-by-token. Exact match or `<*>` wildcard match at each level. | Identical in [tree_router.py](file:///home/amilame/github/Practicum/log-parser-pipeline/component_3_unified_parser/core/logparser_llm/tree_router.py) L81-111. **Extended:** also matches any `<TAG>` node (e.g., `<LOI>`, `<OID>`) not just `<*>`. | ✅ **Faithful + extended** for category-aware matching |
| **Loose Match** | Compare log tokens against all templates of same length. Compute similarity ratio. If > threshold, match. | Identical structure in `loose_match()` L113-146. Filters templates by token count, computes similarity on static tokens only (excludes wildcard positions). **Extended:** optional positional weighting via exponential decay (`weighted_jaccard_similarity`). | ✅ **Faithful + extended** |
| **Loose Match Metric** | Paper uses a structural binary token-alignment procedure: same token count required, then positional token-by-token comparison where `<*>` wildcards match any token. This is a binary match/no-match decision, not a continuous ratio. | Two modes: (1) Standard Jaccard (set-based), (2) Positional weighted Jaccard with decay factor `λ=0.15` (prioritizes early-position tokens). Configurable via `use_positional_weighting`. | ⚠️ **Different mechanism.** Paper uses a binary structural alignment (match or reject); our implementation uses a continuous similarity score with a threshold. See detailed analysis below. |
| **Variable-Aware Prompting** | 10 semantic categories: OID, LOI, OBN, TID, SID, TDA, CRS, OBA, STC, OTP. LLM replaces variables with category tokens. Final output normalizes to `<*>`. | 10 categories in `categories_mode=10`: TDA, LOI, OID, USR, POR, STA, VER, PRO, NUM, COM. **Category names differ.** Toggle to 3 categories (LOI, OID, TDA) available. | ⚠️ **Modified categories.** See detailed analysis below. |
| **ICL Few-Shot** | Similarity-based historical log-template pairs selected as demonstrations. Paper uses cosine similarity between LLM embeddings of the query log and existing examples, selecting the top-k most similar. Also uses 10 seed examples (one per variable category) as initial foundation. | Jaccard-similarity-based top-K retrieval from `template_pool` in [llm_extractor.py](file:///home/amilame/github/Practicum/log-parser-pipeline/component_3_unified_parser/core/logparser_llm/llm_extractor.py) L117-123. Fallback seed examples if pool is empty. `k_shots=3` default. | ✅ **Faithful intent.** Both use similarity-based retrieval (paper: cosine on embeddings, ours: Jaccard on tokens). The similarity metric differs but the selection philosophy (retrieve most relevant examples) is the same. |
| **Prompt Output Format** | Template string with category tokens, normalized to `<*>` afterward | JSON object: `{"template": "...", "variables": [{"category": "<TAG>", "value": "..."}]}`. Category tokens stay in the template (not normalized to `<*>`). | ⚠️ **Different format.** Paper normalizes to `<*>`; we preserve category tokens in the template. This means our prefix tree stores `<LOI>`, `<OID>`, etc. rather than `<*>`. |
| **ECS Field Mapping** | Not present. Paper is academic — no SIEM integration. | Automatic mapping: `<TDA>` → `event.ingested`, `<LOI>` → `source.ip`, `<OID>` → `file.path`, `<USR>` → `user.name`, `<POR>` → `source.port`, etc. Variables extracted from LLM JSON response are written directly to the log record's ECS fields. | ✅ **Novel production feature** not in the paper |
| **Calibration (LogParser-LLM-C)** | Human-in-the-loop: user provides 32 labeled examples as ICL demonstrations. Immediate granularity adjustment without retraining. | Not implemented. No mechanism for injecting user-labeled examples. | ❌ **Missing.** The calibration variant (LogParser-LLM-C) is the paper's most impactful contribution (up to 56.8% PA and 69.7% PTA/FTA improvement). |
| **Template Merging** | Merge structurally similar templates: replace divergent tokens with `<*>`. LLM-assisted semantic merge decisions. | `TemplateManager.calibrate()` in [template_manager.py](file:///home/amilame/github/Practicum/log-parser-pipeline/component_3_unified_parser/core/logparser_llm/template_manager.py): pairwise comparison of same-length templates, token-level structural similarity, mismatch → `<*>` substitution. Threshold: 0.95. **No LLM-assisted merge.** | ⚠️ **Simplified.** Paper uses LLM judgment for merge decisions; we use purely structural heuristics. This may miss semantic merges (different-length templates that are logically equivalent). |
| **Pruning** | Prefix tree acts as cache; paper doesn't describe explicit pruning. | Two pruning mechanisms: (1) `prune_inactive_templates()` — remove templates not matched in 30 days, (2) `prune_to_capacity(max_templates=1000)` — LRU eviction of oldest templates. Both triggered every 1000 logs. | ✅ **Production addition** for memory management |
| **GGD/PGD Metrics** | Novel metrics: Grouping Granularity Distance and Parsing Granularity Distance. Measure minimum merge/split operations to reach ground truth. | Not implemented. | ❌ **Missing.** These metrics are evaluation tools, not parsing features. Could be implemented as a standalone evaluator. |
| **Models Tested** | GPT-4 (primary), GPT-3.5-turbo, Llama-2-13b | Ollama with configurable model: `llama3` (default), `gemma4:26b`, `deepseek-r1:32b`, `qwen3.6:27b` | ⚠️ **Different models.** Paper's benchmark results are with GPT-4. Our results with local models will differ in accuracy. |
| **Template Pool Seeding** | Not explicitly described | Seeds from LogBatcher's cache file (`logbatcher_cache.json`) if available, enabling cross-method knowledge transfer | ✅ **Novel production feature** |

### Detailed Analysis: Category Differences

| Paper's 10 | Our 10 | Alignment |
|------------|--------|-----------|
| OID (Object ID) | OID (Object Identifier) | ✅ Same concept |
| LOI (Location Indicator) | LOI (Location Indicator) | ✅ Identical |
| OBN (Object Name) | COM (System command/component/process) | ⚠️ Partial overlap — OBN covers hostnames/domain names; COM covers processes/commands |
| TID (Type Indicator) | — | ❌ Missing |
| SID (Switch Indicator) | — | ❌ Missing |
| TDA (Time/Duration) | TDA (Time/Date/Activity) | ✅ Same concept |
| CRS (Computing Resources) | NUM (General numeric) | ⚠️ CRS is specific (memory, disk, CPU); NUM is generic |
| OBA (Object Amount) | NUM (General numeric) | ⚠️ Merged into NUM |
| STC (Status Code) | STA (Status codes/outcomes) | ✅ Same concept |
| OTP (Other Parameters) | — | ❌ Missing |
| — | USR (User Information) | ✅ Novel (not in paper) |
| — | POR (Port number) | ✅ Novel (subset of paper's LOI) |
| — | VER (Version info) | ✅ Novel (not in paper) |
| — | PRO (Network protocol) | ✅ Novel (not in paper) |

**Assessment:** Our categories are **production-oriented** (USR, POR, VER, PRO map directly to ECS fields) but **not a faithful reproduction** of the paper's 10 categories. We're missing TID, SID, OTP, and have merged CRS+OBA into generic NUM. The paper's categories were empirically tested on 14 datasets; our substitutions have not been benchmarked.

### Detailed Analysis: Loose Match Metric

The paper's "loose match" is a **structural binary token-alignment procedure**, not a continuous similarity metric:
1. First checks if the incoming log has the **same token count** as the cluster's syntax template (different counts → immediate rejection).
2. If counts match, performs **positional token-by-token alignment** where `<*>` wildcards can align with any single token.
3. After loose match succeeds, **regular expressions** are used to rigorously verify non-wildcard alignments.

This is a binary match/no-match decision. Our implementation instead computes a continuous similarity score and applies a threshold, which is a fundamentally different mechanism:
1. **Standard Jaccard** (`use_positional_weighting=false`): `|intersection| / |union|` of token sets. Order-independent.
2. **Weighted positional** (`use_positional_weighting=true`, default): `Σ w_i × match_i / Σ w_i` where `w_i = exp(-0.15 × i)`.

Neither mode replicates the paper's binary structural alignment. The closest approach would be a strict positional match with wildcard passthrough.

### Where the Paper/Original is Better

1. **GPT-4 backbone:** Paper's benchmarks use GPT-4, which has substantially better parsing accuracy than local 8B-32B models. The paper reports 90.6% GA and 81.1% PA on LogPub. Our local models will score lower. **Impact: High for accuracy.**

2. **Human-in-the-loop calibration (LogParser-LLM-C):** The paper's most impactful contribution. With just 32 labeled examples, improvements of up to 56.8% PA and 69.7% PTA/FTA (template-level accuracy metrics). We have no equivalent. **Impact: High.**

3. **GGD/PGD evaluation metrics:** These provide nuanced granularity assessment beyond binary correct/incorrect. **Impact: Moderate** (evaluation, not parsing).

4. ~~**ICL diversity selection:**~~ *Removed — fact-check confirmed the paper also uses similarity-based selection (cosine similarity, top-k most similar), consistent with our Jaccard-based approach.*

5. **Category system tested on benchmarks:** Paper's 10 categories were evaluated on 14 LogHub datasets. Our modified categories are untested. **Impact: Moderate.**

### Production Proxy Assessment

**Rating: Partial proxy with significant divergences.**

The prefix tree routing (strict → loose → LLM fallback) is faithfully reproduced. The core efficiency mechanism (tree cache eliminates >99.99% of LLM calls) works identically. However:
- The variable-aware prompting uses different categories
- The loose match uses a different similarity metric
- Loose match uses a continuous similarity score vs the paper's binary structural alignment
- The calibration variant (the paper's headline contribution) is entirely missing
- Template output format differs (category tokens vs normalized `<*>`)

The ECS mapping, capacity pruning, and cross-method seeding are valuable production additions with no paper equivalent.

---

## Section 4: Cross-Cutting Concerns

### LLM Client

| Aspect | Original Repos | Our Implementation |
|--------|---------------|--------------------|
| **Backend** | HuggingFace transformers (LibreLog), OpenAI API (LogParser-LLM), OpenAI API / Together AI (LogBatcher) | Unified Ollama HTTP client for all three methods |
| **Token tracking** | Not present | Cumulative `prompt_tokens`, `completion_tokens`, `total_tokens`, `invocations`, `llm_timeouts`, `failed_invocations` |
| **Debug logging** | Not present | Optional `llm_debug.jsonl` with full request/response payloads |
| **Retry logic** | Varies | Uniform 2-attempt retry with timeout tracking |
| **Embedding cache** | Not present | In-memory dict cache for embedding requests |

### Operational Features (Not in Any Original)

| Feature | Description |
|---------|-------------|
| Round-robin dataset interleaving | Prevents any single dataset from monopolizing the parser |
| Time-limit enforcement | `--time-limit` flag with periodic checks and early termination |
| Cross-run cache persistence | `--use-cache` / `--write-cache` for all three methods |
| Profiling output | Per-run JSON profiles with timing, token usage, LLM call counts |
| Progress logging | 10-second interval status with speed, percentage, cache hit ratio |
| Unified CLI | Single `main_parser.py --method {logparser-llm,logbatcher,librelog}` entry point |

---

## Section 5: Summary Verdicts

| Method | Algorithmic Fidelity | Production Readiness | Key Gaps |
|--------|---------------------|---------------------|----------|
| **LogBatcher** | **High** — core pipeline (cluster→sample→LLM→cache→prune) is faithful | **High** — 3-tier noise fallback, LRU cache, candidate capping, time budgets | O(n×m) cache lookup |
| **LibreLog** | **High** — Drain tree, adaptive sampling, self-reflection, regex manager all ported | **Medium-High** — dataset isolation, cache persistence added | `DummyMemory` instead of `LogMemory`; no integrated evaluation |
| **LogParser-LLM** | **Medium-High** — tree routing faithful, ICL strategy consistent with paper, but category system, loose match mechanism, and output format diverge | **High** — capacity pruning, LRU eviction, ECS mapping, cross-method seeding | Missing calibration (LogParser-LLM-C), different categories, binary vs continuous loose match |

### Is Our Repo a Good Production Proxy?

**For LogBatcher: Yes.** The implementation is a production-hardened version of the same algorithm. Any accuracy differences would stem from model choice (local vs cloud), not algorithmic divergence.

**For LibreLog: Yes.** The parsing algorithm is faithfully reproduced. Both the original (HuggingFace transformers) and our backend (Ollama) perform local single-stream inference, so throughput characteristics are comparable.

**For LogParser-LLM: Partially.** The efficiency mechanism (prefix tree routing) is faithful, and the ICL selection strategy is consistent with the paper (both use similarity-based retrieval). However, the prompting layer has differences (categories, output format) and the loose match uses a continuous similarity score rather than the paper's binary structural alignment. Accuracy results are not directly comparable to the paper's GPT-4 benchmarks. The missing calibration variant is the paper's most impactful contribution. Our ECS mapping is a genuine production innovation not found in the paper.
