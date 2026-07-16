# Usage Guide

Practical instructions for running the pipeline — full end-to-end or one component at a time — plus a reference for every `.env` and `config.yaml` option that affects behavior.

---

## 0. Prerequisites

- Docker + Docker Compose, on a **Linux host** — Components 2 and 5 use `network_mode: host` (see below), which Docker Desktop for Mac/Windows doesn't fully support.
- An external Docker network named `search-net` for Components 1, 3, and 4 (it isn't created by this repo's compose files):
  ```bash
  docker network create search-net
  ```
- An Ollama instance reachable on that network (or on the host, via `host.docker.internal`), for Component 3. Not required for Components 1, 2, 4, 5.
- For Components 2 and 5: **Tailscale must already be installed, running, and authenticated on the Docker host itself** (not just reachable from it). These two services use `network_mode: host` specifically so they can use the host's `tailscale0` interface directly — they don't join `search-net` and don't run their own Tailscale client.
- `.env` in the repo root — copy the template first:
  ```bash
  cp .env.example .env
  ```

---

## 1. Full Pipeline (Docker Compose)

Each component is a separate Compose service; there is no single "run everything" command — run them in order:

```bash
# 1. Standardize raw logs to ECS JSONL
docker compose run --rm component_1

# 2. (Optional) Pull live logs from a Security Onion deployment
docker compose run --rm component_2

# 3. Parse — pick one method
docker compose run --rm component_3 python main_parser.py --method logparser-llm --write-cache
# or: --method logbatcher / --method librelog

# 4. Evaluate against ground truth
docker compose run --rm component_4

# 5. (Optional) Deploy parsed templates as a Grok ingest pipeline
docker compose run --rm component_5
```

All five share the same `./data` and `./config.yaml` bind mounts (see `docker-compose.yml`), so output from one stage is automatically visible to the next. Containers run as `${UID}:${GID}` to avoid root-owned output files — if those aren't set in your shell, Compose falls back to `1000:1000`.

### Quick local test (no live Ollama needed)

`docker-compose.test.yml` spins up a mock Ollama server and runs Components 1 → 3 → 4 against a synthetic dataset:

```bash
./run_e2e.sh
```

---

## 2. Component 1 — Dataset Ingestion (`transform_to_ecs.py`)

Converts raw LogHub/BOTSv3 CSVs into standardized ECS JSONL.

```bash
docker compose run --rm component_1
# equivalent to: python transform_to_ecs.py   (reads paths from config.yaml)

# Or override input/output paths explicitly:
docker compose run --rm component_1 python transform_to_ecs.py \
  --loghub data/raw/loghub/some_dataset.csv \
  --botsv3 data/raw/botsv3/botsv3.csv \
  --out-dir data/processed/loghub
```

| Flag | Description |
|---|---|
| `--loghub PATH` | Path to a LogHub CSV (or directory of CSVs). Falls back to `directories.dataset_name`'s default input dir if omitted. |
| `--botsv3 PATH` | Path to a BOTSv3 CSV. Same fallback behavior. |
| `--out-dir PATH` | Output directory for ECS JSONL. Defaults to `directories.output_dir/{dataset_name}`. |

With no flags, behavior is driven entirely by `config.yaml`'s `directories.dataset_name` (`"loghub"` or `"botsv3"`).

---

## 3. Component 2 — Security Onion Extractor (`extract_so_logs.py`)

Pulls dead-letter-queue and unmapped logs from a live Security Onion deployment. No CLI flags — entirely `.env`/`config.yaml` driven.

```bash
docker compose run --rm component_2
```

Requires these `.env` variables (templated in `.env.example` as blank placeholders — fill them in):

| Variable | Required for | Description |
|---|---|---|
| `TAILSCALE_NODE` | DLQ extraction | Tailscale hostname/IP of the Security Onion box |
| `TS_USER` | DLQ extraction | SSH user for the Tailscale connection (default `admin`) |
| `TS_PASS` | DLQ extraction (optional) | SSH password. Leave blank to use SSH-agent/key-based auth instead — required if the target uses Tailscale SSH, which has no password to supply. |
| `SO_IP` | Elasticsearch extraction | Security Onion's IP/host |
| `SO_USER` | Elasticsearch extraction | ES basic-auth username |
| `SO_PASS` | Elasticsearch extraction | ES basic-auth password |

If `TAILSCALE_NODE` is unset, DLQ extraction is skipped (logged as a warning, not an error). Same for `SO_IP`/`SO_USER`/`SO_PASS` and ES extraction — the two extraction paths are independent. If `TS_PASS` is unset or blank, `extract_dlq_logs()` connects without a password, falling back to Paramiko's default SSH-agent/key-file lookup.

DLQ extraction reads both Logstash pipeline dead-letter directories over SSH: `/nsm/logstash/dead_letter_queue/main/*` and `/nsm/logstash/dead_letter_queue/search/*`.

Runs with `network_mode: host` (see Prerequisites) so it can reach `TAILSCALE_NODE` over the host's Tailscale interface.

Output now lands in `data/processed/{dataset_name}/` (matching Component 1's layout, so Component 3 picks it up automatically with no manual move) as `so_dlq_logs.jsonl` and `unmapped_fallback_logs.jsonl`. Each record is wrapped with a `message` field and a dataset-prefixed `event.id` (`so_dlq_N` / `so_unmapped_N`), the same contract Component 3 expects from Component 1's ECS output.

> [!NOTE]
> Logstash's dead-letter-queue segment files are a binary format, not plain JSON — raw `cat` output isn't guaranteed to be meaningful text. The wrapping above guarantees valid JSONL either way, but garbled DLQ content will show up as a garbled `message` value rather than a real log line. For a correct decode, drain the DLQ through Logstash's own `dead_letter_queue` input plugin (e.g. a pipeline that reads the DLQ and writes JSON to a file) and point extraction at that output instead of the raw segment files — that's a Security Onion / Logstash configuration change, not something this script can do by itself.

> [!NOTE]
> The remote `cat` command runs via `sudo` over a non-interactive SSH exec — the SSH user needs passwordless (`NOPASSWD`) sudo rights for it on the Security Onion box, independent of whatever SSH auth method (key, agent, password, or Tailscale SSH) gets you logged in. If sudo prompts for a password interactively, this will hang or fail. Scope the sudoers entry to exactly the commands needed rather than granting blanket NOPASSWD access:
> ```
> # /etc/sudoers.d/so_extractor
> your_ssh_user ALL=(root) NOPASSWD: /bin/cat /nsm/logstash/dead_letter_queue/main/*, /bin/cat /nsm/logstash/dead_letter_queue/search/*
> ```
> Alternatively, skip sudo entirely by making the DLQ directories group-readable for the SSH user, if your Security Onion permission model allows it.

`config.yaml`'s `extractor.batch_size` (default 5000) and `extractor.lookback_time` (default `now-24h`) control the Elasticsearch Scroll API query.

> [!WARNING]
> This component uses `verify=False` for TLS and `paramiko.AutoAddPolicy()` for SSH — see README §6 before pointing this at anything beyond a lab environment.

---

## 4. Component 3 — Unified Parser (`main_parser.py`)

```bash
docker compose run --rm component_3 python main_parser.py --method logparser-llm [flags]
```

`--method` is required; the other flags apply to whichever method you pick.

| Flag | Applies to | Description |
|---|---|---|
| `--method {logparser-llm,logbatcher,librelog}` | all | Required. Selects the parsing algorithm. |
| `--use-cache` | all | Load templates/memory from `data/cache/{dataset}/` before parsing (warm start). |
| `--write-cache` | all | Save discovered templates/memory back to `data/cache/{dataset}/` on exit. |
| `--time-limit SECONDS` | all | Stop parsing early once this many seconds have elapsed; still writes partial output. |
| `--icl-selection-strategy {similarity,diversity}` | logparser-llm | Overrides `logparser_llm.icl_selection_strategy` from `config.yaml` for this run. |
| `--llm-debug` | all | Also log raw LLM request/response payloads to `llm_debug.jsonl`. |

Examples:

```bash
# Cold run, write cache for next time, cap at 25 seconds
docker compose run --rm component_3 python main_parser.py --method logparser-llm --time-limit 25 --write-cache

# Warm run from an existing cache
docker compose run --rm component_3 python main_parser.py --method logbatcher --use-cache --write-cache

# Switch models for a single run without editing .env
docker compose run --rm -e OLLAMA_MODEL=gemma component_3 python main_parser.py --method librelog
```

Output per method (under `data/parsed/{dataset}/`):

| Method | Output file | Profile file |
|---|---|---|
| `logparser-llm` | `parsed_{input}.jsonl` (one per input file) | `parsed_{input}_profile.json` |
| `logbatcher` | `logbatcher_output.csv` | `logbatcher_profile.json` |
| `librelog` | `librelog_output.csv` | `librelog_profile.json` |

`logbatcher` also appends regex-pre-masked (low-confidence) logs to `data/parsed/quarantine.jsonl` for audit.

---

## 5. Component 4 — Metric Evaluation (`evaluate_metrics.py`)

No CLI flags — reads everything from `config.yaml`.

```bash
docker compose run --rm component_4
```

Compares whatever is in `data/parsed/{dataset}/` against ground truth in `data/raw/{dataset}/`, for every method that has output present. Writes all three outputs to `data/results/{dataset}/{model}/{shortdatetime}.{ext}` (folders created automatically), where `{model}` joins every distinct `model_used` across the parsers evaluated in this run (e.g. `qwen3.6-27b+gemma4-26b` if they differ) and `{shortdatetime}` is `%y%m%d_%H%M`:

- `{shortdatetime}.json` — raw metrics per method (GA, FGA, PA, FTA, ED, GGD, PGD, PMSS, cache-hit rate, throughput).
- `{shortdatetime}_viz.json` — chart-ready summary.
- `{shortdatetime}.html` — self-contained interactive dashboard (open directly in a browser).

Each of these also gets copied into `data/archive/{dataset}/{model_used}/{method_used}/` (one folder per individual parser, timestamped filenames) — this older, per-parser archival step is unchanged and now just copies from the new `data/results/` location instead of a fixed flat path.

`evaluator.nrows` in `config.yaml` caps how many ground-truth rows are read per file (`null` = all).

---

## 6. Component 5 — Grok Ingest Deployer (`main_deployer.py`)

No CLI flags. Compiles `data/parsed/parsed_loghub_ecs.jsonl` into a Grok ingest pipeline and deploys it.

`core/compiler.py`'s `TAG_TO_GROK` maps every category tag from all three `logparser_llm.categories_mode` options (`paper_10`, `ecs_10`, `ecs_3`) to a Grok macro, so the compiled pipeline works regardless of which mode produced the input templates. Any tag genuinely outside that set falls back to being left as literal text in the pattern (won't match real content at that position).

```bash
docker compose run --rm component_5
```

Requires these four `.env` variables — the process exits immediately if any are missing:

| Variable | Description |
|---|---|
| `SO_IP` | Security Onion Elasticsearch host |
| `SO_USER` | ES basic-auth username |
| `SO_PASS` | ES basic-auth password |
| `TAILSCALE_NODE` | Tailscale hostname for the SaltStack SFTP upload |

Plus these two, used for the SaltStack SFTP/SSH leg specifically (separate credentials from the ES ones above — not required, both have defaults):

| Variable | Default | Description |
|---|---|---|
| `TS_USER` | `admin` | SSH user for the SaltStack SFTP connection. |
| `TS_PASS` | — (optional) | SSH password. Leave blank to use SSH-agent/key-based auth instead — required if the target uses Tailscale SSH, which has no password to supply. |

Runs with `network_mode: host` (see Prerequisites) so it can reach `TAILSCALE_NODE` over the host's Tailscale interface.

> [!NOTE]
> The remote `mv`/`chown` commands run via `sudo` over a non-interactive SSH exec — same requirement as Component 2: the SSH user needs passwordless (`NOPASSWD`) sudo rights on the Security Onion box. Scope the sudoers entry to the specific commands/paths rather than granting blanket access (adjust to match your `saltstack.tmp_dir`/`destination_dir`/`file_owner` config):
> ```
> # /etc/sudoers.d/so_deployer
> your_ssh_user ALL=(root) NOPASSWD: /bin/mv /tmp/*.json /opt/so/saltstack/local/salt/elasticsearch/files/ingest/*.json, /bin/chown so-elasticsearch\:so-elasticsearch /opt/so/saltstack/local/salt/elasticsearch/files/ingest/*.json
> ```

`config.yaml`'s `deployer` section controls behavior:

| Key | Default | Description |
|---|---|---|
| `dry_run` | `false` | If `true`, compiles and validates the pipeline but skips the actual ES PUT / SFTP upload. |
| `pipeline_name` | `"so_custom_ingest_pipeline"` | Name of the Elasticsearch ingest pipeline. |
| `elasticsearch.port` | `9200` | ES port. |
| `elasticsearch.verify_certs` | `false` | TLS cert verification — see README §6 before enabling in production without a proper CA. |
| `saltstack.tmp_dir` | `"/tmp/"` | Remote staging directory before the file is moved into place. |
| `saltstack.destination_dir` | `/opt/so/saltstack/local/salt/elasticsearch/files/ingest/` | Final SaltStack ingest file location. |
| `saltstack.file_owner` | `"so-elasticsearch:so-elasticsearch"` | Ownership applied to the uploaded file. |

> [!NOTE]
> The parsed-log input path (`/app/data/parsed/parsed_loghub_ecs.jsonl`) is currently hardcoded to the `loghub` dataset regardless of `directories.dataset_name` — if you're deploying from a `botsv3` run, copy/symlink the output to that path first.

---

## 7. `.env` Reference

| Variable | Default | Used by | Description |
|---|---|---|---|
| `OLLAMA_API_BASE` | `http://ollama:11434/api` | Component 3 | Ollama endpoint. Use the native `/api` suffix, not `/v1` — required for `"think": false` on reasoning models (see README §4). |
| `OLLAMA_MODEL` | `qwen` | Component 3 | Maps through `qwen`→`qwen3.6:27b`, `gemma`→`gemma4:26b`, `deepseek`→`deepseek-r1:32b`, or `llama3`→`llama3` (unmapped values pass through as-is). |
| `OLLAMA_TIMEOUT` | `90` | Component 3 | Per-request timeout, in seconds. |
| `OLLAMA_EMBED_BASE` | same as `OLLAMA_API_BASE` | Component 3 | Optional — override only if embeddings are served from a different endpoint than chat completions. Not in `.env.example`; add it only if needed. |
| `USE_CACHE` | `false` | Component 3 | Default for `--use-cache` if the flag isn't passed explicitly. |
| `WRITE_CACHE` | `false` | Component 3 | Default for `--write-cache` if the flag isn't passed explicitly. |
| `LLM_DEBUG` | `false` | Component 3 | Default for `--llm-debug` if the flag isn't passed explicitly. |
| `TAILSCALE_NODE` | — | Components 2, 5 | Tailscale hostname of the Security Onion box (SSH target for both). |
| `TS_USER` | `admin` | Components 2, 5 | SSH user for the Tailscale connection — DLQ extraction (Component 2) and SaltStack SFTP upload (Component 5). |
| `TS_PASS` | — (optional) | Components 2, 5 | SSH password for that same connection. Blank falls back to SSH-agent/key auth — required for Tailscale SSH targets. Independent of `SO_PASS` below. |
| `SO_IP` | — | Components 2, 5 | Security Onion Elasticsearch host. |
| `SO_USER` | — | Components 2, 5 | Elasticsearch basic-auth username — unrelated to the SSH login above. |
| `SO_PASS` | — | Components 2, 5 | Elasticsearch basic-auth password — unrelated to the SSH login above. |

All variables above ship in `.env.example` as blank placeholders except `OLLAMA_EMBED_BASE` (optional, add it yourself only if needed). If you're not running Components 2 or 5, the `TAILSCALE_NODE`/`TS_USER`/`TS_PASS`/`SO_IP`/`SO_USER`/`SO_PASS` lines can stay blank — those extraction/deploy steps just get skipped with a warning (Component 2) or a hard error at startup (Component 5, which requires `SO_IP`/`SO_USER`/`SO_PASS`/`TAILSCALE_NODE` specifically).

---

## 8. `config.yaml` Reference

### `directories`

| Key | Default | Description |
|---|---|---|
| `dataset_name` | `"loghub"` | `"loghub"`, `"botsv3"`, or a custom name. All other paths resolve as `{base_dir}/{dataset_name}/`. |
| `input_dir` | `data/raw` | Base raw-data directory. |
| `output_dir` | `data/processed` | Base ECS-JSONL output directory. |
| `cache_dir` | `data/cache` | Base cache directory. |

### `extractor` (Component 2)

| Key | Default | Description |
|---|---|---|
| `batch_size` | `5000` | Elasticsearch Scroll API page size. |
| `lookback_time` | `now-24h` | How far back to query for unmapped logs. |

### `llm`

| Key | Default | Description |
|---|---|---|
| `api_base` | `http://localhost:11434/api` | Fallback only — `OLLAMA_API_BASE` in `.env` always wins when set, and every documented way of running this pipeline (`docker compose run`) loads `.env` via `env_file:`. This key only matters if `main_parser.py` is invoked directly without `.env` sourced. |

### `logparser_llm` (Component 3, `--method logparser-llm`)

| Key | Default | Description |
|---|---|---|
| `k_shots` | `3` | Number of ICL few-shot examples per LLM call. |
| `loose_match_threshold` | `0.8` | Minimum similarity score to accept a loose-match template. |
| `categories_mode` | `"paper_10"` | `"paper_10"` (paper's own 10 categories — faithful reproduction), `"ecs_10"` (opt-in ECS-oriented 10-category set), or `"ecs_3"` (LOI/OID/TDA only). |
| `calibration_file` | `""` (disabled) | Path to a JSON file of 32 labeled `{template, ref_log}` examples to seed as ICL demonstrations (LogParser-LLM-C style). Empty string disables it. |
| `icl_selection_strategy` | `"similarity"` | `"similarity"` (Jaccard top-k) or `"diversity"` (max-min diversity sampling). |
| `loose_match_metric` | `"positional_uniform"` | `"positional_uniform"` (positional match ratio, closest to the paper), `"positional_decay"` (exponential-decay-weighted Jaccard), or `"jaccard"` (plain set-based Jaccard). |
| `decay_factor` | `0.15` | Decay coefficient λ, only used by `"positional_decay"`. |
| `merge_similarity_threshold` | `0.95` | Structural similarity required to merge two same-length templates during calibration. |

> Removed: `embedding_model` used to be listed here, but this method's ICL retrieval uses plain token Jaccard similarity, never embeddings — the key was read into the shared `OllamaClient`'s default state but never actually consulted for an API call. Only `logbatcher.embedding_model` below is genuinely used, since LogBatcher is the only method that calls the embedding endpoint (`DPPSampler`).

### `logbatcher` (Component 3, `--method logbatcher`)

| Key | Default | Description |
|---|---|---|
| `batch_size` | `10` | DPP/sampler output size — number of representative logs sent to the LLM per cluster. |
| `sampler` | `"DPPSampler"` | `"DPPSampler"`, `"SimilarSampler"` (medoid + k-nearest), or `"RandomSampler"`. |
| `cluster` | `"SimilarityCluster"` | `"SimilarityCluster"` (DBSCAN, default) or `"LengthCluster"` (cheaper, no DBSCAN cost). |
| `vectorizer` | `"binary"` | `"binary"` (Jaccard distance) or `"tfidf"` (cosine distance) — only affects `SimilarityCluster`. |
| `use_dynamic_eps` | `false` | Scale DBSCAN's `eps` by token-length standard deviation instead of using a fixed value. |
| `noise_max_retries` | `2` | Re-queue attempts for DBSCAN noise logs before falling back to regex pre-masking. |
| `embedding_model` | `"nomic-embed-text"` | Ollama embedding model name (used by `DPPSampler`). |
| `embedding_length_threshold` | `4000` | Character-length cutoff above which `DPPSampler` skips the embedding call and routes the log through the cheaper Jaccard diverse-selection fallback (same algorithm as `SimilarSampler`) instead of substituting a random embedding vector. Any other embedding failure (e.g. a transient API error) is routed the same way. Set to `null` to always attempt embedding (embedding failures still fall back). Also sets `OllamaClient`'s truncation length when non-null, so the two stay in sync. |
| `cluster_similarity_threshold` | `0.8` *(not currently in config.yaml — code default)* | DBSCAN similarity threshold (`eps = 1 - threshold`). Add this key under `logbatcher:` to override. |
| `buffer_max_size` | `500` *(not currently in config.yaml — code default)* | Micro-batch flush trigger by log volume. |
| `flush_timeout_seconds` | `5.0` *(not currently in config.yaml — code default)* | Micro-batch flush trigger by elapsed time. |

### `librelog` (Component 3, `--method librelog`)

| Key | Default | Description |
|---|---|---|
| `similarity_threshold` | `0.5` | Fallback Drain `st` (grouping similarity threshold) for any dataset name not covered by the built-in per-dataset `DATASET_SETTINGS` (16 LogHub sub-datasets). Datasets in that list (HDFS, BGL, Apache, etc.) still use their own tuned `st`/`depth` values regardless of this key. Matches the code's own hardcoded fallback, so this is a no-op at its default — set it explicitly to change grouping strictness for unlisted/custom dataset names. |
| `max_memory_size` | `null` (unbounded) | Max entries kept in the LLM template memory (`DummyMemory`) before the oldest is evicted (FIFO). `null` matches the original unbounded behavior; set an integer to cap memory growth on long/large runs. |
| `k_shots` | `3` | Number of representative logs sampled per cluster (`regex_sample`) for the LLM template-extraction prompt. |
| `enable_reflection` | `true` | Enables the self-reflection retry loop (validate generated regex against group logs, re-prompt on mismatches, up to 3 rounds). Set `false` to disable and accept the first LLM-generated template as-is. |
| `use_drain_backup` | `false` | If `true`, load a cached Drain-tree grouping pass from `data/cache/{dataset}/librelog_drain_backup_{dataset}.json` instead of re-running Drain. |
| `write_drain_backup` | `true` | If `true`, save the Drain grouping pass to that same file after running it. |

> `similarity_threshold`, `max_memory_size`, `k_shots`, and `enable_reflection` were previously present in `config.yaml` but not read by any code — they're now wired into `LibreLogParser.__init__` (see `parser_implementation_comparison.md` for history). `config.yaml`'s values match the code's own prior hardcoded defaults, so out of the box nothing changes behaviorally — these keys are now genuinely load-bearing if you choose to change them.

### `evaluator` (Component 4)

| Key | Default | Description |
|---|---|---|
| `nrows` | `null` | Row limit per ground-truth file (`null` = read all rows). |

### `deployer` (Component 5)

See the table in §6 above.

---

## 9. Tests

```bash
pytest tests/          # unit tests
./run_e2e.sh            # full mock-Ollama integration run (Components 1 → 3 → 4)
```
