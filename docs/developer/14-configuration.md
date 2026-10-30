---
name: 14. Configuration reference
route: /configuration
menu: Operations
---

# 14. Configuration reference

Every setting the code reads, with its default and meaning. Settings come
from four places:

| source | read by | how |
|---|---|---|
| `Settings` classes (pydantic-settings) | the backend (`backend/app/config.py`), rc-kg (`rc-kg/config.py`) | environment variable of the same name, any case; then `.env` in the current directory; then the default |
| `os.environ` reads | backend and rc-kg modules, for paths and switches outside `Settings` | environment only |
| shell `${VAR:-default}` | launch scripts (`cluster/`, `rc-kg/scripts/`, the OOD templates) | environment only |
| the OOD form | `ood-apps/rc-copilot/form.yml` | what the member picks when launching |

In a session you rarely set any of these yourself: the OOD app and the
launch scripts set them. This chapter is for knowing what they set, and for
running pieces by hand.

## 14.1 Backend: `Settings` (`backend/app/config.py`)

`get_settings()` reads the environment (and `backend/.env`, when uvicorn runs
from `backend/`) **once** per process and caches it; restart the backend to
change a setting. Unknown variables are ignored.

### Model server

| variable | default | meaning |
|---|---|---|
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | the OpenAI-compatible endpoint, including `/v1`. The OOD app sets `http://127.0.0.1:$VLLM_PORT/v1` |
| `VLLM_MODEL_NAME` | `meta-llama/Llama-3.1-8B-Instruct` | the model id as `/v1/models` lists it. The OOD app sets the served name (14.4) |
| `VLLM_API_KEY` | unset | only if the server was started with `--api-key`; the client sends `EMPTY` otherwise |
| `TEMPERATURE` | `0.2` | generation temperature; low keeps the JSON stable |
| `MAX_TOKENS` | `1400` | the reply's token budget; raise it if drafts are truncated |
| `REQUEST_TIMEOUT_SECONDS` | `120.0` | per model request |
| `USE_PROXY_ENV` | `false` | `false`: the model host is added to `no_proxy`/`NO_PROXY` at startup (`llm_client.ensure_proxy_bypass`), because Explorer's site proxy refuses internal traffic. `true` only to reach a hosted API through a proxy |

### Authentication

| variable | default | meaning |
|---|---|---|
| `AUTH_TOKEN` | unset | the access token. Unset: one is minted at startup and printed in the banner. The OOD app sets OOD's session password |
| `SESSION_HOURS` | `3.0` | how long a sign-in lasts. The OOD app sets the job's hours |
| `AUTH_DISABLED` | `false` | no sign-in at all; only for a backend on `127.0.0.1` on your own machine (chapter 11) |

### Knowledge graph

| variable | default | meaning |
|---|---|---|
| `RC_KG_URL` | unset | the rc-kg API. Unset: drafts are not grounded, and say so |
| `RC_KG_API_TOKEN` | unset | bearer token for rc-kg; the session generates it |
| `RC_KG_K` | `6` | sources put in the prompt per ticket |
| `RC_KG_TIMEOUT_SECONDS` | `15.0` | per rc-kg call; only matters when rc-kg is wedged |

### Ticket context, lookups, playbooks

| variable | default | meaning |
|---|---|---|
| `TICKET_CONTEXT_CLEANING` | `true` | clean the scraped ticket (notifications, address lists, signatures, quoted chains out; oldest first) |
| `TICKET_MAX_TOKENS` | `6000` | the most of the prompt the ticket may take; it also always fits what the context window leaves |
| `CLUSTER_LOOKUPS` | `true` | run the read-only lookups |
| `CLUSTER_LOOKUP_TIMEOUT_SECONDS` | `5.0` | per command |
| `PLAYBOOKS` | `true` | match playbooks and put their runbooks in the prompt |

### Tracing, evaluation, logging

| variable | default | meaning |
|---|---|---|
| `TRACE_TOKEN_BREAKDOWN` | `true` | count each prompt part's tokens with vLLM's `/tokenize` for the run log (one local request per part) |
| `ONLINE_EVAL` | `true` | the serving model grades each draft in the background (one extra model call) |
| `ONLINE_EVAL_SAMPLE_RATE` | `1.0` | share of drafts graded |
| `DEBUG` | `false` | **logs full ticket text and responses**; warns at startup. Local debugging only |
| `LOG_FILE` | `logs/rc_copilot.log` | log file, relative to the working directory; `""` = stdout only (the OOD app sets `""`) |
| `APP_PORT` | `8080` | port, used only by `python -m app.main` |

### rc-agent only

> **rc-agent:** these exist only in rc-agent's `config.py`.

| variable | default | meaning |
|---|---|---|
| `AGENT_MODE` | `true` | run the agent graph between gathering and writing. `false`: exactly TicketMate's pipeline |
| `AGENT_MAX_STEPS` | `4` | model turns per specialist agent, after its plan |
| `AGENT_MAX_TOOL_CALLS` | `8` | tool calls per draft, in all |
| `AGENT_MAX_REVISIONS` | `1` | times the critic or reviewer may send a draft back |
| `AGENT_REVIEWER` | `true` | run the reviewer agent (one extra model call per draft) |
| `KG_CONTEXT_MAX_CHARS` | `7000` | characters of knowledge-graph sources the writer reads, packed by rank (`context_pack.py`) |

## 14.2 Backend: environment read directly

These are read with `os.environ` at the point of use, not through `Settings`,
so they are not in `.env` unless your shell exports them.

| variable | default | read in | meaning |
|---|---|---|---|
| `RUN_LOG_DIR` | `<repo>/traces/$USER/runs` | `runlog.py` | where run logs go; `""` turns recording off |
| `RC_TRACES_DIR` | `<repo>/traces/$USER`, or `/scratch/$USER/rc_copilot_traces` if the repository is not writable | `ops_store.py` | root of spans, metrics, guardrail findings and eval reports (and of run logs when `RUN_LOG_DIR` is unset) |
| `RC_TRACES_SPANS` | `on` | `otel.py` | `off`/`0`/`false`/`no`: do not write spans to `traces/$USER/spans/` |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | unset | `otel.py` | set either to also export spans over OTLP/HTTP to a collector |
| `OTEL_SERVICE_NAME` | `rc-agent` | `otel.py` | the spans' `service.name` |
| `OTEL_EXPORT_CONTENT` | unset | `otel.py` | `1`/`true`/`yes`: put prompts and outputs (ticket text) in spans |
| `PLAYBOOK_DIR` | `backend/app/playbooks` (the example) | `playbooks.py` | the playbook directory; `cluster/env.sh` points it at the private playbook directory |
| `RC_GROUP_XTRA` | `/shared/admin/ROOT/etc/group.xtra` | `lookups.py` | RC-managed groups not in `/etc/group` |
| `RC_LDCONFIG` | `/sbin/ldconfig` | `lookups.py` | the `ldconfig` used for library lookups (tests point it at a fake) |
| `VLLM_LOG_FILE` | unset | `startup.py` | vLLM's log, parsed for loading progress; the OOD app sets it |
| `RC_KG_PROGRESS_DIR` | unset | `startup.py` | rc-kg's startup checklist directory; the OOD app sets it |
| `SLURM_JOB_ID` | `local` / `n/a` | `main.py`, `runlog.py` | the trace's session id; recorded in run logs |
| `USER` | `unknown` | `ops_store.py`, `runlog.py` | names the traces directory; recorded with feedback |
| `RC_COPILOT_RELEASE` | unset | `main.py` | recorded as `release` in the trace's version block |
| `APP_HOST` | `127.0.0.1` | `main.py` `__main__` | host, only for `python -m app.main` (uvicorn's `--host` otherwise) |

> **rc-agent:** `RC_AGENT_PROMPTS` (default unset, read in
> `agents/prompt_registry.py`): a directory whose `*.md` files replace
> same-named agent prompts. Used by `evals/prompt_ab.py` to A/B a prompt
> variant.

## 14.3 The OOD app form (`ood-apps/rc-copilot/form.yml`)

| field | widget | default | choices |
|---|---|---|---|
| `bc_num_hours` (Hours) | number | `1` | 1-8; capped by the partition |
| `partition` | select | `gpu-interactive` | `gpu-interactive` (2 h), `gpu-short` (2 h), `gpu` (8 h), `multigpu` (24 h, needs approval) |
| `gpu_layout` (GPUs) | select | `any` | `any`, `t4`, `1x80`, `1x40`, `2x`, `4x` |
| `rc_model` (Model) | select | `t4-7b` | `t4-7b`, `t4-14b`, `small`, `qwen7b`, `flash`, `air` |

`submit.yml.erb` turns these into Slurm options:

| layout | `--gres` | `--constraint` | `--mem` | `--cpus-per-task` |
|---|---|---|---|---|
| `any` | `gpu:1`, with `--exclude` of every node that cannot run it (V100s), built from `sinfo` at submit time | | 48G | 8 |
| `t4` | `gpu:t4:1` | | 48G | 8 |
| `1x40` | `gpu:a100:1` | | 80G | 12 |
| `1x80` | `gpu:1` with `--exclude` of non-80 GB nodes (else `gpu:a100:1`) | `a100@80g` when no node list | 120G | 16 |
| `2x` | `gpu:a100:2` | | 200G | 16 |
| `4x` | `gpu:a100:4` | | 380G | 16 |

The model overrides the layout: `air` -> `4x`; `t4-*` -> `t4` unless `any`
was picked; `qwen7b` -> `1x40`; `flash` -> `1x80`; `small` -> `any`. `2x` and
`4x` force partition `multigpu`. Hours are capped at the partition's limit
(2, 2, 8, 24) and at least 1. The job is named `rc-copilot`.

### What the session script sets

`template/before.sh.erb` exports `port`, `VLLM_PORT` and `RC_KG_PORT` (OOD's
`find_port`) and `password` (`create_passwd`). `template/script.sh.erb` then:

| variable | value |
|---|---|
| `TICKETMATE_HOME` | from `ticketmate.env`, written by `publish.sh` beside the template; the script exits if it is unset |
| `TM_ROOT`, `RC_KG_ROOT` | `$TICKETMATE_HOME/ticketmate`, `$TICKETMATE_HOME/rc-kg` unless set |
| `RC_KG_VARIANT` | `full` unless set |
| `TP`, `MAX_LEN` | by layout: `any`/`t4` 1/16384; `1x40`/`1x80` 1/32768; `4x` 4/131072; otherwise 2/131072. By model, overriding: `t4-7b` 1/16384; `t4-14b` 1/8192; `qwen7b` 1/32768; `small` 1/16384; `air` 4/131072 |
| `RC_KG_PROGRESS_DIR` | `<session dir>/rc_kg_progress` (mode 0700) |
| `RC_KG_API_TOKEN` | random, `secrets.token_urlsafe(24)` |
| `RC_KG_URL` | `http://127.0.0.1:$RC_KG_PORT` |
| `VLLM_LOG_FILE` | `<session dir>/vllm.log` |
| `VLLM_BASE_URL` | `http://127.0.0.1:$VLLM_PORT/v1` |
| `VLLM_MODEL_NAME` | the served name for the model (`qwen2.5-7b-instruct-awq` for `t4-7b`, ...); replaced by what vLLM actually serves if they differ |
| `AUTH_TOKEN` | `$password` |
| `SESSION_HOURS` | the form's hours |
| `LOG_FILE` | `""` |
| `USE_PROXY_ENV` | `false` |

The backend listens on `0.0.0.0:$port`; vLLM is started with
`VLLM_HOST=127.0.0.1`.

## 14.4 Launch scripts (`ticketmate/cluster/`)

### `env.sh` (sourced by every launcher)

| variable | default | meaning |
|---|---|---|
| `TM_ROOT` | the checkout's `ticketmate/` | |
| `TICKETMATE_HOME` | the repository root | shared toolchain and caches |
| `HF_HOME` | `$TICKETMATE_HOME/.cache/huggingface` | model weights |
| `HF_HUB_OFFLINE` | `1` | never contact the Hub from a compute node; set `0` on a login node to download |
| `VLLM_CACHE_ROOT`, `OUTLINES_CACHE_DIR`, `TRITON_CACHE_DIR` | under `$TICKETMATE_HOME/.cache/` if writable, else `/scratch/$USER/rc_copilot_cache/` | compile and grammar caches |
| `UV_CACHE_DIR` | `$TICKETMATE_HOME/.cache/uv`, or the `/scratch` fallback | |
| `PLAYBOOK_DIR` | the private playbook directory, when it exists | |
| `CUDA_HOME` | the cluster's CUDA 12.8.0 module tree, when present | |

### `serve_glm.sh` (vLLM)

| variable | default | meaning |
|---|---|---|
| `MODEL` | `flash` | `flash`, `air`, `qwen7b`, `qwen14b`, `t4-7b`, `t4-14b`, `small` (chapter 2.4) |
| `PORT` | `8000` | |
| `VLLM_HOST` | `0.0.0.0` | **pass `127.0.0.1`**; the OOD app and `serve_stack.sbatch` do |
| `TP` | per model | tensor parallelism |
| `MAX_LEN` | per model | `--max-model-len` |
| `DTYPE` | `float16` on compute capability below 8, else `auto` | |
| `GLM_PARSERS` | `1` for GLM models | GLM tool and reasoning parsers |

> **rc-agent:** `QWEN_TOOLS` (default `1`): Qwen models get
> `--enable-auto-tool-choice --tool-call-parser hermes`; `0` turns it off.

### Others

| script | variable | default | meaning |
|---|---|---|---|
| `serve_stack.sbatch` | `MODEL` | `small` | |
| | `SESSION_HOURS` | `3` | |
| | `VLLM_PORT` | `8000` | |
| | `RC_KG_ROOT`, `RC_KG_VARIANT` | `../rc-kg`, `full` | |
| | `OOD_HOST`, `OOD_ROUTE` | `ood.explorer.northeastern.edu`, `rnode` | the URL it prints |
| | `TM_ROOT` | from Slurm's record of the script | |
| `install_vllm.sh` | `VLLM_VERSION`, `CUDA_TAG`, `PY_VERSION` | `0.29.0`, `cu129`, `3.12` | which vLLM build |
| `tests/run_inference_test.sbatch` | `MODEL`, `TICKET`, `INSTRUCTIONS` | `small`, `tests/sample_ticket.txt`, empty | |
| `capture.sbatch` | `CAPTURE_DIR`, `SESSION_HOURS` | `/scratch/$USER/rc_copilot_captures`, `4` | the capture-test app |
| `traces.py` | `RUN_LOG_DIR`, `RC_TRACES_DIR` | the backend's run-log directory (above) | default `--dir` |
| `ood-apps/publish.sh` | `RC_COPILOT_RELEASES` | `<repo>/rc-copilot` | release directory; 3 releases kept |

## 14.5 rc-kg: `Settings` (`rc-kg/config.py`)

Read from the environment, then `.env` in the current directory, then the
default. `serve_kg.sh` and `build_graph.sh` `cd` into their instance
directory first, so a developer's `rc-kg/.env` never reaches a session or a
build; they are configured by the environment alone. `.env.example` lists
the development values.

### Sources

| variable | default | meaning |
|---|---|---|
| `WP_API` | `https://rc.northeastern.edu/wp-json/wp/v2` | the RC website's WordPress API |
| `SITEMAP_INDEX` | `https://rc.northeastern.edu/wp-sitemap.xml` | its sitemap |
| `USER_AGENT` | `RC-KG/0.1 (+Northeastern Research Computing; <contact address>)` | identifies the crawler to the site's admins |
| `REQUEST_DELAY` | `1.0` | seconds between requests to the site |
| `REQUEST_TIMEOUT` | `30.0` | seconds |
| `DOCS_ENABLED` | `true` | also ingest rc-docs. Set by variant: `full` -> `true`, `website` -> `false` |
| `DOCS_BASE` | `https://rc-docs.northeastern.edu/en/latest/` | |
| `DOCS_REPO` | `https://github.com/northeastern-rc/rc-public-documentation` | for last-commit dates |
| `DOCS_BRANCH` | `master` | |
| `DOCS_SOURCE_DIR` | `docs/source` | |

### Neo4j

| variable | default | meaning |
|---|---|---|
| `NEO4J_URI` | `bolt://127.0.0.1:7687` | set per instance by `_kg_instance.sh` |
| `NEO4J_USER` | `neo4j` | |
| `NEO4J_PASSWORD` | `""` | random per instance; the driver refuses to start without one |
| `NEO4J_DATABASE` | `neo4j` | |

### Embeddings and LLM

| variable | default | meaning |
|---|---|---|
| `EMBEDDING_PROVIDER` | `sentence_transformers` | or `openai`, `vllm`, `openai_compatible` |
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | |
| `EMBEDDING_DIM` | `384` | must match the model and the vector index |
| `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY` | unset | for a remote embedder |
| `LLM_MODEL` | `Qwen/Qwen2.5-7B-Instruct` | only for `POST /answer` |
| `LLM_BASE_URL` | unset | `/answer` works only when set |
| `LLM_API_KEY` | unset | |
| `LLM_TEMPERATURE` | `0.1` | |
| `LLM_MAX_TOKENS` | `1200` | |

### Behaviour and serving

| variable | default | meaning |
|---|---|---|
| `INCLUDE_EVENTS_IN_RETRIEVAL` | `false` | past events stay in the graph but out of results |
| `RESOLVE_REDIRECTS` | `true` | follow redirects on internal links before storing them |
| `DATA_DIR` | `data` | |
| `RC_KG_API_TOKEN` | unset | bearer token for every endpoint but `/health`; unset = open |
| `RC_KG_MANIFEST` | unset | the served snapshot's `manifest.json`, shown by `/health`; `serve_kg.sh` sets it |

## 14.6 rc-kg: scripts and environment

### Serving (`scripts/serve_kg.sh`, `scripts/_kg_instance.sh`, `api/app.py`)

| variable | default | meaning |
|---|---|---|
| `RC_KG_PORT` | a free port (`--port` overrides) | API port; host is `127.0.0.1` (`--host` overrides) |
| `RC_KG_VARIANT` | `full` | `full` or `website` (`--variant`); a snapshot of the other variant is refused |
| `RC_KG_SNAPSHOT` | the variant's directory under the data root | directory holding `neo4j.dump` |
| `RC_KG_DATA_ROOT` | `/projects/rc/projects/rc-kg-data` if it exists, else `rc-kg/data` | where snapshots are read and published (`graph/`, `graph-website/`) |
| `RC_KG_TMP` | `$TMPDIR`, else `/tmp` | node-local parent of instance directories |
| `RC_KG_THREADS` | `2` | `OMP_NUM_THREADS`/`MKL_NUM_THREADS` for query embedding, so torch leaves cores to vLLM |
| `RC_KG_PROGRESS_DIR` | unset | startup checklist, one `<step>.json` per step; also where `refresh.log` and failed-start Neo4j logs go |
| `RC_KG_JAVA_HOME` | unset | a JDK 17+ to use; else `JAVA_HOME`, JDK 21/17, `java` on `PATH`, the cluster's OpenJDK |
| `RC_KG_NEO4J_WAIT` | `300` | seconds to wait for Bolt to open |
| `RC_KG_WARM_TIMEOUT` | `900` | seconds the API waits for an embedded graph before warm-up fails |
| `HF_HUB_OFFLINE` | `1` | the embedding model is read from `rc-kg/vendor/hf`, never fetched while serving |
| `RC_KG_ROOT` | the checkout's `rc-kg/` | |

Fixed in code: `POST /admin/swap` closes the old driver after 60 s
(`SWAP_GRACE_S`).

### Refresh loop (`scripts/refresh_kg.sh`)

| variable | default | meaning |
|---|---|---|
| `RC_KG_REFRESH_LOOP` | `on` | read by `serve_kg.sh`: `off` serves the starting snapshot only (every test sets this) |
| `RC_KG_REFRESH` | `on` | `off`: never rebuild, still switch to newer snapshots |
| `RC_KG_REFRESH_MAX_AGE_H` | `24` | rebuild when the published snapshot is this many hours old |
| `RC_KG_REFRESH_CHECK_MIN` | `20` | minutes between checks |
| `RC_KG_REFRESH_FIRST_MIN` | `2` | minutes before the first check |
| `RC_KG_REFRESH_LOG` | `$RC_KG_PROGRESS_DIR/refresh.log`, else in the instance directory | |

A build lock older than 4 hours, or left on the same node by a process that
is gone, is removed.

### Building and tools

| script | variable or flag | default | meaning |
|---|---|---|---|
| `build_graph.sh` | `--variant`, `--all` | `full` | which graph(s) |
| | `--min-hit` | `0.9` | eval gate on hit@5 |
| | `--force` | off | publish below the gate (never past a failed integrity check) |
| | `RC_KG_SNAPSHOT` | the variant's directory | publish target; not allowed with `--all` |
| `rebuild.sbatch` | `RC_KG_ROOT` | from Slurm's record of the script | |
| `start_neo4j.sh`, `stop_neo4j.sh` | `RC_KG_DATA_DIR` | `/tmp/rc_kg_neo4j` | the single-developer Neo4j |
| `install_neo4j.sh` | `NEO4J_VERSION` | `5.26.0` | |
| `with_kg.sh`, `eval/variants.py` | `RC_KG_VARIANT` | `full` | which graph to load |
| `eval/ragas_llm.py` | `JUDGE_URL`, `JUDGE_API_KEY`, `RC_KG_URL`, `RC_KG_API_TOKEN` | unset, `EMPTY`, unset, unset | the judge model and the rc-kg to evaluate |

## 14.7 Test-only settings

| variable | default | used by | meaning |
|---|---|---|---|
| `STUB_REJECT_SCHEMA` | unset | `tests/rc_kg/stub_vllm.py` | reject `json_schema` requests with 400 |
| `STUB_NO_TOOLS` | unset | `stub_vllm.py` | refuse tool calls (rc-agent scenario G) |
| `STUB_MAX_MODEL_LEN` | `16384` | `stub_vllm.py` | the context length it advertises |
| `RC_CONTRACT_KEEP_RUNS` | unset | `run_contract.sh` | copy the run logs there before cleanup |
| `RC_SLOW_START_TIMELINE` | unset | `run_slow_start.sh` | save the `/api/health` timeline |
| `PW_PYTHON` | `/scratch/$USER/pw_venv/bin/python` | `tests/ui/run.sh` | Playwright's Python |
| `PLAYWRIGHT_BROWSERS_PATH` | `/scratch/$USER/pw_browsers` | `tests/ui/run.sh` | Playwright's Chromium |
| `SHOTS` | `/tmp` | `tests/ui/run.sh` | screenshots |
