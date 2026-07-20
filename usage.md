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

> [!NOTE]
> Security Onion firewalls its Elasticsearch REST port (9200) by default — connections from outside its configured host groups are silently dropped rather than refused, which looks identical to a generic network/DNS problem from this side. To let the pipeline host (or its Tailscale subnet) reach it, on the Security Onion box go to **Advanced → Firewall → HostGroups → `elasticsearch_rest`**, add the pipeline host's IP/subnet, then **Synchronize firewall**. This gates Component 2's `extract_unmapped_logs()` Elasticsearch pull and *all* of Component 5 (`es_client.py`/`validator.py` both call the ES REST API directly to register and validate the ingest pipeline) — see their sections below. It does not gate Component 2's DLQ extraction or Component 5's SaltStack SFTP upload, since both of those go over Tailscale SSH rather than the ES REST port.

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

> [!NOTE]
> `extract_unmapped_logs()` hits Elasticsearch's REST API directly and needs the pipeline host's subnet added to Security Onion's `elasticsearch_rest` firewall host group — see Prerequisites. DLQ extraction (`extract_dlq_logs()`) is unaffected, since it goes over Tailscale SSH rather than the ES REST port.

DLQ extraction lists every Logstash pipeline's dead-letter directory over SSH via a wildcard (see note below on why this isn't a hardcoded list), reading only closed segments (`*.log` — the segment Logstash is actively appending to has a `*.log.tmp` suffix and is skipped), fetches each one's raw bytes individually, and decodes each through a vendored Go binary (see note below) rather than treating the binary segment format as plain text.

Runs with `network_mode: host` (see Prerequisites) so it can reach `TAILSCALE_NODE` over the host's Tailscale interface.

Output lands in `data/processed/{dataset_name}/` (matching Component 1's layout, so Component 3 picks it up automatically with no manual move) as `so_dlq_logs.jsonl` and `unmapped_fallback_logs.jsonl`. Each record is wrapped with a `message` field and a dataset-prefixed `event.id` (`so_dlq_N` / `so_unmapped_N`), the same contract Component 3 expects from Component 1's ECS output.

> [!NOTE]
> `dataset_name` here is `config.yaml`'s `extractor.dataset_name` if set, else it falls back to `directories.dataset_name`. Use the `extractor`-scoped key when you want Component 2 to pull into a different dataset folder than whatever Components 1/3/4/5 are currently pointed at, without having to flip the global setting back and forth.

> [!NOTE]
> Logstash's dead-letter-queue segment files are a binary format (version byte + 32KB-block framing + a length-prefixed, CBOR-encoded event), not plain JSON. `extract_dlq_logs()` decodes them properly using [`logstash-dlq-decode`](https://github.com/saj/logstash-dlq-decode) (MIT, pinned to commit `078993e2` — no tagged releases exist — reviewed for safety and vendored into the Component 2 Docker image at `/usr/local/bin/logstash-dlq-decode` via a `golang:alpine` build stage), rather than reading raw bytes as if they were text.
>
> Two things fall out of how the format works: (1) the tool has to run **once per segment file**, never on concatenated bytes — each segment has its own version byte and block framing that would misalign across files, which is why extraction lists and fetches segments individually instead of using a single `cat {glob}`. (2) a segment that fails to decode (truncated mid-record — most likely because it's the one Logstash is still writing, though `*.log.tmp` files are already excluded from the listing) is logged as a warning and skipped, not fatal to the rest of the run.
>
> The decoded event lands in a Java/JRuby CBOR shape (`["org.logstash.ConvertedMap", {...}]`-style class-tagged pairs); `_unwrap_cbor_tagged()` strips that down to plain Python types, then the record's own `message` field becomes the output `message`, and the DLQ's own rejection reason (why Logstash couldn't index it) is carried through as `event.reason` alongside `event.id`/`event.dataset`.

> [!NOTE]
> The extractor reads `/nsm/logstash/dead_letter_queue/*/*` — a wildcard over every pipeline's DLQ subdirectory, not a hardcoded list of pipeline names. Security Onion installations don't agree on what those subdirectories are called (`main` vs `manager` have both been observed across different installs/versions), so hardcoding specific names silently misses data on whichever installations don't match the guess. Using a directory-level wildcard sidesteps that entirely — it picks up every pipeline's DLQ folder regardless of naming.

> [!NOTE]
> By default the remote `cat` runs **without** `sudo` (`config.yaml`'s `extractor.dlq_use_sudo: false`). The recommended setup is group-based read access: make the SSH user a member of whatever group owns the DLQ directories, so no privilege escalation capability is granted at all —
> ```
> ls -la /nsm/logstash/dead_letter_queue/          # confirm the owning group
> sudo usermod -aG <that_group> your_ssh_user      # e.g. logstash
> ```
> Group membership is read at SSH session start, so a fresh `ssh`/pipeline connection picks it up immediately — no logout/login needed on an interactive shell for this to work, since the pipeline always opens a new connection per run anyway.
>
> If your host can't grant group access and you still need `sudo`, set `extractor.dlq_use_sudo: true` in `config.yaml`. This now needs NOPASSWD rights on **two** command shapes — the segment-listing `sh -c` and the per-file `cat` — which makes exact sudoers wildcard-matching considerably more fragile than a single command was:
> ```
> # /etc/sudoers.d/so_extractor
> your_ssh_user ALL=(root) NOPASSWD: /bin/sh -c ls /nsm/logstash/dead_letter_queue/*/*.log 2>/dev/null, /bin/cat /nsm/logstash/dead_letter_queue/*/*
> ```
> Before trusting this in an automated run, verify both non-interactively: `ssh your_ssh_user@host "sudo -n sh -c 'ls /nsm/logstash/dead_letter_queue/*/*.log 2>/dev/null'"` and `sudo -n cat` against one real segment path returned by that listing. `sudo -n` fails immediately instead of hanging on a password prompt if a NOPASSWD grant isn't matching correctly. Given this fragility, a wrapper script (e.g. `/usr/local/bin/so_dlq_list.sh` / `so_dlq_cat.sh`) with NOPASSWD granted on the exact script paths is more robust than trying to get the wildcard patterns to match cleanly — but honestly, group-based access (above) avoids this whole problem and is the tested, working path.

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

No CLI flags. Compiles `data/parsed/parsed_loghub_ecs.jsonl` (or `deployer.parsed_logs_path` if set — see below) into a Grok ingest pipeline and deploys it.

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
> Component 5 talks to Security Onion over **two separate paths with two separate access requirements**: `es_client.py`/`validator.py` hit the Elasticsearch REST API directly (`https://{SO_IP}:9200`) to register and validate the ingest pipeline, which needs the pipeline host's subnet added to Security Onion's `elasticsearch_rest` firewall host group (see Prerequisites) — the same requirement as Component 2's ES pull. Separately, `salt_sftp.py` pushes the compiled pipeline file over Tailscale SSH/SFTP, which is unaffected by that firewall rule (different port, already covered by Tailscale). Both need to work for a deploy to succeed.

> [!NOTE]
> The remote `mv`/`chown` commands run via `sudo` over a non-interactive SSH exec — same requirement as Component 2: the SSH user needs passwordless (`NOPASSWD`) sudo rights on the Security Onion box. Scope the sudoers entry to the specific commands/paths rather than granting blanket access (adjust to match your `saltstack.tmp_dir`/`destination_dir`/`file_owner` config). Since sudoers matches the literal command text, the `chown` target here must match `file_owner` **exactly** — including across installations: a real test against a live SO box found no `so-elasticsearch` user at all, only a plain `elasticsearch` user/group (confirm with `id elasticsearch` — or whatever user your ES process actually runs as — on your own box rather than assuming):
> ```
> # /etc/sudoers.d/so_deployer
> your_ssh_user ALL=(root) NOPASSWD: /bin/mv /tmp/*.json /opt/so/saltstack/local/salt/elasticsearch/files/ingest/*.json, /bin/chown elasticsearch\:elasticsearch /opt/so/saltstack/local/salt/elasticsearch/files/ingest/*.json
> ```
> If `file_owner` and this sudoers rule fall out of sync (e.g. after changing one but not the other), the failure mode differs by which side is wrong: a `chown` target unmatched by sudoers hangs/fails on "a password is required" (non-interactive sudo can't prompt); a `file_owner` naming a user that doesn't exist on the box at all fails with "invalid user" — both leave the file already moved into `saltstack.destination_dir` (the `mv` half of the compound command already succeeded) but with unintended ownership, needing a manual `chown` to correct.

`config.yaml`'s `deployer` section controls behavior:

| Key | Default | Description |
|---|---|---|
| `dry_run` | `false` | If `true`, compiles and validates the pipeline but skips the actual ES PUT / SFTP upload. |
| `pipeline_name` | `"so_custom_ingest_pipeline"` | Name of the Elasticsearch ingest pipeline. |
| `parsed_logs_path` | `""` (→ `/app/data/parsed/parsed_loghub_ecs.jsonl`) | Overrides the input JSONL path. Blank uses the default; set to any other path (e.g. a hand-picked single-record file) to debug the compile/simulate/deploy flow against one specific template without touching the real output. |
| `elasticsearch.port` | `9200` | ES port. |
| `elasticsearch.verify_certs` | `false` | TLS cert verification — see README §6 before enabling in production without a proper CA. |
| `saltstack.tmp_dir` | `"/tmp/"` | Remote staging directory before the file is moved into place. |
| `saltstack.destination_dir` | `/opt/so/saltstack/local/salt/elasticsearch/files/ingest/` | Final SaltStack ingest file location. |
| `saltstack.file_owner` | `"elasticsearch:elasticsearch"` | Ownership applied to the uploaded file. Verify the actual owning user on your own SO box (`id elasticsearch`) rather than assuming — installations vary. |

> [!NOTE]
> The default parsed-log input path (`/app/data/parsed/parsed_loghub_ecs.jsonl`) is hardcoded to the `loghub` dataset regardless of `directories.dataset_name` — if you're deploying from a `botsv3` run, either copy/symlink the output to that path first, or point `parsed_logs_path` at the `botsv3` output directly.
>
> To test with a single hand-picked template (useful for debugging the compile → `/_simulate` → deploy flow without noise from a full run): write a one-line JSONL file with just `message` and `parsed_template` fields — e.g. `data/parsed/debug_single_template.jsonl` — and set `parsed_logs_path: "/app/data/parsed/debug_single_template.jsonl"`. Combine with `dry_run: true` to validate the Grok pattern against real Elasticsearch (via `/_simulate`) without actually deploying anything.

### 6.1 Registering a pipeline is not the same as it applying to new logs

`main_deployer.py` only registers `so_custom_ingest_pipeline` as a named resource in Elasticsearch (plus persisting the file via SaltStack) — it does **not** wire that pipeline into any live ingest chain. By itself, a freshly-deployed pipeline sits unused; nothing routes documents through it until something explicitly references it. Confirmed by direct investigation against a live cluster: neither Fleet-managed index templates nor any `default_pipeline`/`final_pipeline` setting anywhere in the cluster referenced it after a real deploy.

The one confirmed-working way to make it apply to genuinely new incoming logs is `wire_global_custom.py` (§6.2 below) — a **separate, explicitly-invoked script**, not part of `main_deployer.py`'s default flow. It's kept separate deliberately: `global@custom` is a Security-Onion-owned, cluster-wide pipeline (it runs on nearly every document across nearly every data stream via a chain that both Fleet-integration pipelines and Security Onion's own native `syslog`/`common` pipeline converge on), a much larger blast radius than the isolated `so_custom_ingest_pipeline` `main_deployer.py` already manages. Folding it into every default deploy would mean every routine template push also re-touches that shared resource.

> [!NOTE]
> **`wire_global_custom.py` has been exercised against a live Security Onion cluster** — confirmed working end-to-end: idempotent merge against the live `global@custom` definition, pre-flight `/_simulate`, ES PUT, SaltStack persistence, and survival of a forced `sudo so-elasticsearch-pipelines` re-push (re-fetched afterward and the routing processor was still present). Still run `dry_run: true` first on any new cluster you point this at — it's proven against one live cluster, not a guarantee every SO installation's `global@custom` and sudoers setup will line up identically.
>
> Two real failure modes surfaced during that first live run, both now handled/documented:
> - **Persistence can lag behind ES state.** The original idempotency check treated "ES already matches" as proof persistence had also succeeded, so a run that completed Step A (ES PUT) but failed Step B (SaltStack, e.g. a missing sudoers grant) would report "nothing to do" on retry and never actually finish persisting. Fixed: the idempotent-no-op path now still re-attempts Step B unconditionally, only skipping the redundant ES PUT (Step A).
> - **Sudoers colon-escaping is not optional.** A live sudoers entry using `elasticsearch:elasticsearch` (unescaped) rather than `elasticsearch\:elasticsearch` failed with a sudoers syntax error, not a permissions error — confirms the escaping shown in the example below is a hard requirement, not defensive boilerplate.

### 6.2 Component 5 — Wiring into live ingest (`wire_global_custom.py`)

```bash
docker compose run --rm component_5 python wire_global_custom.py
```

Fetches `global@custom`'s *current* definition from Elasticsearch, checks whether a `pipeline` processor already routes to the target pipeline, and if not, appends one and PUTs it (Step A). Deliberately never hardcodes what Security Onion's own baseline processors in `global@custom` should look like — always reads the live definition and appends to it, so a future Security Onion update to that file isn't silently reverted by this script.

Idempotent, but not a plain no-op on a repeat run: if ES already routes to the target pipeline, Step A (the PUT) is skipped, but **Step B (SaltStack persistence) always re-runs anyway** — ES state matching doesn't prove the file was successfully persisted on a prior run (see the sudoers/partial-failure note above), so a repeat run doubles as a safe way to confirm or repair persistence without touching the live pipeline again.

If `global@custom` doesn't exist at all on the target cluster, the script refuses to create one from scratch and exits with an error — its baseline processors are Security-Onion-owned, and inventing a stripped-down replacement would be worse than doing nothing.

Same two-pronged deployment pattern as `main_deployer.py`, with one critical difference in the persistence step: `global@custom` has **no `.json` extension** on disk (`so-elasticsearch-pipelines`, Security Onion's own pipeline-push script, uses the filename itself as the Elasticsearch pipeline name — a `.json` suffix would push to the wrong pipeline name entirely, `global@custom.json`). `salt_sftp.py` now has two methods: `deploy_persistently()` (unchanged, still appends `.json`, used by `main_deployer.py`) and `deploy_persistently_exact()` (new, uses the filename verbatim, used only by this script).

Requires the same `.env` variables as `main_deployer.py` (§6 above), plus its own sudoers grant — **the existing `*.json` wildcard grant does not cover this file**:
```
# /etc/sudoers.d/so_deployer (add alongside the existing so_custom_ingest_pipeline grant)
your_ssh_user ALL=(root) NOPASSWD: /bin/mv /tmp/global@custom /opt/so/saltstack/local/salt/elasticsearch/files/ingest/global@custom, /bin/chown elasticsearch\:elasticsearch /opt/so/saltstack/local/salt/elasticsearch/files/ingest/global@custom
```
Exact-match rather than wildcard, since `global@custom` is always the same literal filename (unlike `so_custom_ingest_pipeline`, whose filename varies with `deployer.pipeline_name`).

`config.yaml`'s `deployer.global_custom` section controls this script specifically:

| Key | Default | Description |
|---|---|---|
| `target_pipeline` | `""` (→ `deployer.pipeline_name`) | Which pipeline `global@custom` should route unmapped logs into. Blank uses the same pipeline `main_deployer.py` deploys. |
| `condition` | `"ctx.event?.category == null"` | Painless `if` gate on the appended processor. The default mirrors Component 2's own definition of "unmapped" (`NOT _exists_:event.category`), so only logs the standard pipeline left uncategorized get routed through `target_pipeline` — already-parsed documents are untouched. |

**Why this is the only reachable hook, not one option among several:** investigated directly against a live cluster. A per-index `final_pipeline` setting doesn't survive the data stream's ILM rollover (reverts silently). A competing higher-priority index template would need to faithfully replicate the existing template's entire settings/mappings/ILM-policy reference to avoid silently breaking them, and would permanently drift out of sync with Security Onion's own template on every SO update. `logs@custom` (the standard Fleet-integration extension point) is real and safe, but unreachable for logs arriving via Security Onion's native `syslog`/`common` pipeline — only Fleet-managed integration pipelines call it. `global@custom` is the one point both paths converge on.

**This registered-but-unwired persistence gap is separate from another one:** even a successful `wire_global_custom.py` run only edits the *live* Elasticsearch pipeline immediately (Step A) and the SaltStack-managed file (Step B) for durability going forward. If a change to `global@custom` were ever made through the Elasticsearch API directly (bypassing this script entirely — e.g. an ad-hoc `PUT` for a one-off test), it would **not** survive Security Onion's own `so-elasticsearch-pipelines` script, which unconditionally re-pushes every file under `/opt/so/saltstack/local/salt/elasticsearch/files/ingest/` (including its own shipped `global@custom`) on every highstate. Always go through this script — never a bare API `PUT` — for any change intended to be permanent.

### 6.3 Component 5 — Reverting the live-ingest wire (`unwire_global_custom.py`)

```bash
docker compose run --rm component_5 python unwire_global_custom.py
```

Exact inverse of `wire_global_custom.py`: fetches `global@custom`'s current definition, strips out the one `pipeline` processor routing to `target_pipeline` (via `core/global_custom_wirer.py`'s `remove_wired_pipeline()`, the symmetric counterpart to `build_wired_pipeline()`), and — if a matching processor was found — runs the same pre-flight `/_simulate` plus two-pronged ES PUT / SaltStack persist as the wire script. Purely subtractive: every other processor in `global@custom`, including any Security-Onion-added since the wire, is left exactly as currently deployed. Idempotent — if nothing routes to `target_pipeline`, it reports that and exits without touching anything.

Same `.env` requirements and sudoers grant as `wire_global_custom.py` (§6.2) — no separate setup needed, it reuses the identical SaltStack persistence path (`deploy_persistently_exact()`).

Confirmed working against a live cluster in a dry-run (`dry_run: true`, correctly reported "nothing to revert" against an unwired `global@custom`) but **not yet exercised as a real revert** — the live wire it was built to undo has stayed in place. Run `dry_run: true` first and inspect the printed reverted pipeline before a real revert, same caution as the wire script itself. No dedicated unit test coverage yet for `remove_wired_pipeline()` (the existing `tests/test_component_5.py` coverage is for the wire direction only).

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

See the table in §6 above for `dry_run`/`pipeline_name`/`parsed_logs_path`/`elasticsearch.*`/`saltstack.*`, and §6.2 for `global_custom.*` (used by both `wire_global_custom.py` and `unwire_global_custom.py` — §6.3 — not the main deploy).

---

## 9. Tests

```bash
pytest tests/          # unit tests
./run_e2e.sh            # full mock-Ollama integration run (Components 1 → 3 → 4)
```
