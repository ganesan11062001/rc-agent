---
name: 4. The backend request pipeline
route: /backend
menu: The pipeline
---

# 4. The backend request pipeline

The backend is one FastAPI application, `ticketmate/backend/app/main.py`,
run by uvicorn inside the engineer's Slurm job. It is the only process of
the stack that listens on the network. This chapter walks through it: the
endpoints, sign-in, `POST /api/draft` step by step, how failures are
handled, startup, and what runs concurrently. Chapters 5-10 go into each
step's module.

## 4.1 The application

`main.py` at import time:

| what | how |
|---|---|
| `_TM_VERSION` | `git rev-parse --short HEAD` of the checkout, `-dirty` if `git status --porcelain -- backend` is non-empty, and `RC_COPILOT_RELEASE` from the environment; recorded in every trace |
| `_PROMPT_VERSION` | `tracing.prompt_version()`: a 12-character SHA-256 of every upper-case string constant in `prompts.py`, so any wording change is a new version |
| `_GPU` | the GPU names from `nvidia-smi --query-gpu=name`, for traces; `""` without one |
| logging | `configure_logging()`: stdout, plus `LOG_FILE` when set and openable (a read-only log directory does not stop the app); DEBUG level when `DEBUG=true` |
| `app` | `FastAPI(title="RC Copilot", lifespan=lifespan)` |
| static files | `backend/web/` mounted at `/` **last**, so the `/api/*` routes win; `html=True` serves `index.html` |

`lifespan` logs the configuration, warns when `DEBUG=true` (*full ticket text
WILL be written to the logs*), and **prints** (not logs) `auth.startup_banner()`,
so a token can never land in `LOG_FILE`.

Three exception handlers keep every error the browser sees in one shape,
`{error, detail}`:

| exception | status | body |
|---|---|---|
| `RequestValidationError` | 422 | *That request wasn't valid.* / *Paste some ticket text before drafting a response.* |
| `HTTPException` | its own | `{error: exc.detail, detail: null}`, with its headers |
| any other `Exception` | 500 | *Something went wrong on the RC Copilot server.* / *Check the server logs for details.* The stack trace is logged, never sent |

Run directly (`python -m app.main`), it starts uvicorn with `reload=True` on
`APP_HOST` (default `127.0.0.1`) and `APP_PORT` (default 8080). In a session,
`script.sh.erb` runs `python -m uvicorn app.main:app --host 0.0.0.0 --port
$port`: one process, one event loop (4.9).

## 4.2 Endpoints

| method, path | auth | request | response |
|---|---|---|---|
| `POST /api/login` | none | `LoginRequest {access_token}` (1-512 chars) | `LoginResponse {session_token, expires_at, session_hours}`; 401 *That access token isn't right.* |
| `POST /api/draft` | session | `DraftRequest` | `DraftResponse`; 401; 502 `{error, detail, request_id}` when the model fails |
| `POST /api/feedback` | session | `FeedbackRequest` | `FeedbackResponse {feedback_id, request_id}`; 404 when no recorded draft has that id |
| `GET /api/health` | none | -- | `HealthResponse`, always 200 |
| `GET /` and static files | none | -- | the paste-a-ticket page (4.11) |

The request and response models are in `app/models.py`:

| model | fields |
|---|---|
| `DraftRequest` | `ticket_text` (1-200,000 chars, required); `extra_instructions` (≤ 10,000, default `""`); `ticket_number` (≤ 64, default `""`) |
| `DraftResponse` | the model's `LLMDraft` (`problem_summary`, `suggested_steps`, `confidence` 0-1, `caveats`, `draft_response`) plus `confidence_label` (derived: ≥ 0.8 high, ≥ 0.6 medium, else low), `model`, `latency_ms`, `references`, `kg_status`, `context`, `lookups`, `request_id`, `usage`, `evaluations` |
| `FeedbackRequest` | `request_id` (`^[0-9a-f]{8}$`), `key` (`^[a-z][a-z0-9_]{0,39}$`), `score` (-1..1 or null), `value` (≤ 40), `comment` (≤ 2,000), `correction` (≤ 50,000) |
| `HealthResponse` | `status` (`ok` / `unreachable`), `auth_required`, `vllm_base_url`, `configured_model`, `model_available`, `available_models`, `detail`, `knowledge_graph`, `knowledge_graph_detail`, `startup` |

`confidence_label` is computed by the server, not asked of the model, so the
thresholds are enforced in one place (`models.confidence_label`).

## 4.3 Sign-in and sessions

`app/auth.py`. The backend listens on `0.0.0.0` on a GPU node that every
cluster user can reach, so OOD's SSO is not the only gate: every request that
does work needs a session.

- **Access token.** `AUTH_TOKEN` from the environment. In an OOD session it is
  OOD's generated `password` (chapter 3). Unset, the backend generates one
  with `secrets.token_urlsafe(24)` and prints it in the startup banner. With
  `AUTH_TOKEN` set, the banner deliberately does not print it: under Slurm
  stdout becomes a job log on the group-readable `/projects`.
- **Sign-in.** `check_access_token` compares with `hmac.compare_digest`
  (constant time) after stripping the candidate.
- **Session token.** `mint_session()` returns `"<expiry>.<signature>"`:
  the Unix expiry (`now + SESSION_HOURS * 3600`) and the first 32 hex
  characters of an HMAC-SHA256 of it. The key is `secrets.token_urlsafe(32)`,
  made on first use and kept in memory only.
- **Checking.** `require_session` (a FastAPI dependency on `/api/draft` and
  `/api/feedback`) wants `Authorization: Bearer <token>`.
  `verify_session` checks the signature first, then that the expiry is in the
  future. A missing header: *Sign in to draft responses.*; a bad or expired
  token: *Your session has expired. Sign in again.* (both 401).

Sessions are signed rather than stored: there is no table to sweep and
nothing to persist. Restarting the backend changes the key and so ends every
session, which the module docstring calls correct behaviour, not a
limitation. `script.sh.erb` sets `SESSION_HOURS` to the job's hours, so the
UI never claims more time than the job has.

`AUTH_DISABLED=true` skips the check entirely; the banner then says that
anyone who can reach the port can use it. It is only for a backend bound to
`127.0.0.1` on your own machine (the tests use it).

## 4.4 `POST /api/draft`, step by step

`draft(request)` in `main.py`, TicketMate. Each step opens a run in the
draft's run tree (`tracing.run(name, type)`, chapter 10).

| # | step | code | what happens |
|---|---|---|---|
| 0 | start | `draft` | `request_id = uuid4().hex[:8]`; log the sizes (the text itself only with `DEBUG`); `thread = runlog.ticket_folder(ticket_number, ticket_text)`; open the root run `draft` (type `chain`) with metadata: request and thread ids, `session_id` = `SLURM_JOB_ID` or `local`, project, node, GPU, model, prompt version, code versions |
| 1 | clean | `ticket_context.prepare` | run `ticket_context.prepare` (tool). With `TICKET_CONTEXT_CLEANING=false`, a `Prepared` of the stripped text with no query. `ticket = prepared.text` (chapter 5) |
| 2 | lookups, started | `lookups.run` | when `CLUSTER_LOOKUPS` is on, an `asyncio` task runs the read-only lookups on the cleaned ticket, with `CLUSTER_LOOKUP_TIMEOUT_SECONDS` per command, inside a `cluster.lookups` run (chapter 6) |
| 3 | retrieve | `kg_client.retrieve` | meanwhile, retrieval on `prepared.query` (or the whole ticket when the query is empty). Never raises: the result's `status` is `ok`, `unavailable` or `disabled` (chapter 9) |
| 4 | lookups, joined | | await the task; `lookups.prompt_text(found)` becomes the prompt's lookups block; tag `lookups:<n>` |
| 5 | playbooks | `playbooks.find` | with `PLAYBOOKS` on, match RC's procedures against the query, the ticket and the instructions; `prompt_text`, `missing_text`; tags `playbook:<name>`, `playbook:missing_info` (chapter 7) |
| 6 | sources or not | `prompts.instruction_wants_sources` | when the engineer's instructions ask for a follow-up or closure, the sources are left out of the prompt (their ~2k tokens go to the thread); they are still retrieved and traced; tag `kg:sources_not_needed` |
| 7 | budget | `llm_client.context_window` | the served model's `max_model_len` from `/v1/models`, cached per model name; `None` if the server does not say |
| 8 | fit | `ticket_context.fit` | `reserved` = `MAX_TOKENS` + estimated tokens of the system prompt, sources, lookups and playbook block + 2 × the instructions (they appear twice) + 700 for templates. Estimates are characters / `CHARS_PER_TOKEN` (3.0). `budget = TICKET_MAX_TOKENS` without a window, else `max(800, min(TICKET_MAX_TOKENS, window - reserved))`. The ticket is fitted to `budget × 3` characters; `context_info` records the stats (tag `ticket_trimmed` if anything was cut) |
| 9 | trace metadata | | the rc-kg version and snapshot into the trace; `project` becomes `rc-copilot/<graph variant>` or `rc-copilot/no-kg`; tag `kg:<status>` |
| 10 | generate | `llm_client.generate_draft` | inside a `generate_draft` run (chain): build the messages and call vLLM, falling back across structured-output strategies; validated as `LLMDraft` (chapter 8). On `LLMError`: record the failure and return **502** `{error, detail, request_id}` |
| 11 | token breakdown | `llm_client.token_breakdown` | with `TRACE_TOKEN_BREAKDOWN` on, count each prompt part with vLLM's `/tokenize` (best effort); `_token_summary` totals usage over **every** model call, fallbacks included |
| 12 | citations | `kg_client.apply_citations` | when `kg.status == "ok"`: `[S#]` markers to numbered links, unretrieved URLs removed (playbook URLs are also allowed); a low-confidence retrieval with no reference adds a caveat. Otherwise markers are stripped; `unavailable` adds the caveat *The RC knowledge graph was unavailable (...)* (chapter 10) |
| 13 | response | `DraftResponse.from_llm_draft` | plus `references`, `lookups`, `context` |
| 14 | evaluate | `evaluators` | `assertions` (the prompt's rules as code), `metrics`, and `answer_relevance` (an embedding similarity from rc-kg, awaited); each becomes feedback on the trace; failed names in `evaluations.assertions_failed`; tag `assertion_failed` |
| 15 | finish | | `latency_ms`, `kg_status`, `request_id`, `usage`; log one `draft ok` line; automatic feedback (`grounded`, `references`, `citations_flagged`, `urls_removed`, `retrieval_low_confidence`, `needed_fallback`) |
| 16 | record | `record()` | close the run tree; `otel.export` it; `runlog.record` the run log. If it was written, `evaluators.schedule_judge` starts the LLM judge in the background |
| 17 | metrics | `ops_store.append("metrics", ...)` | one row, no ticket text: model, mode `pipeline`, kg status, sources, references, URLs removed, playbooks, lookups, failed assertions, confidence, latency, timings, tokens, prompt version |

`timings` holds milliseconds for `lookups`, `retrieve`, `llm`, `citations`
and `total`; they go into the run log and the metrics row.

> **rc-agent:** the same endpoint with four additions.
> After step 1, `guardrails.check_input` (run `guardrails.input`): chat
> template control tokens are removed from the text and the query, and
> findings are tagged `guardrail:<rule>` (chapter 16).
> Step 3 is `kg_client.retrieve_multi(query, prepared.expansion, subject)`:
> a second search in documentation words when the ticket's symptoms have them.
> Steps 5-10 move into `write_with(evidence, revision_notes, route)`, which
> also packs the sources by rank to `KG_CONTEXT_MAX_CHARS` (7,000) and
> reserves the revision notes in the budget. With `AGENT_MODE` on (the
> default), `agent_graph.run(write_with, ...)` routes the ticket, lets an
> agent call tools, writes, reviews and may call `write_with` again to revise;
> off, `write_with` runs once, exactly TicketMate's pipeline. Everything after
> reads what the last write used (`ws`).
> After step 12, `guardrails.check_output` redacts credentials and turns
> unfixed findings into caveats; every finding goes to
> `traces/$USER/guardrails/`. The root metadata adds the agent and pipeline
> settings; the project is `rc-agent/<variant>`; the run log gains `agent`
> and `guardrails`; the metrics row gains the route, tool calls, refusals,
> reviews and guardrails. Chapters 15-17.

## 4.5 The model client, in brief

`app/llm_client.py` (chapter 8 has the details).

- `get_client()` builds an `openai.AsyncOpenAI` per call, on
  `VLLM_BASE_URL`, with `VLLM_API_KEY` or `"EMPTY"`,
  `REQUEST_TIMEOUT_SECONDS` (120) and `max_retries=1`, and closes it after.
- `ensure_proxy_bypass()` adds the model host to `no_proxy`/`NO_PROXY` unless
  `USE_PROXY_ENV=true`: Explorer's nodes set `http_proxy`, and the site proxy
  refuses internal traffic. It extends `NO_PROXY` instead of passing
  `trust_env=False`, because every httpx version honours it.
- `check_health()` lists `/v1/models`; it never raises and returns
  `(reachable, model_ids, detail)`.
- `generate_draft()` raises `LLMError(message, detail)`, whose message is
  safe to show in the UI: timeout, connection refused, credentials, model not
  served, HTTP error, no completion, output cut off at `MAX_TOKENS`, or no
  valid JSON after every strategy.

## 4.6 Errors: degrade, never fail

Only one thing makes a draft request fail: the model. Everything else
degrades and says so.

| what goes wrong | what happens |
|---|---|
| the request is invalid (empty text, too long) | 422 |
| no session, or expired | 401; the extension signs in again and retries once |
| rc-kg off (`RC_KG_URL` unset) | `kg_status: disabled`; markers stripped; no caveat |
| rc-kg down, slow, or an incompatible API version | `kg_status: unavailable`; the draft is produced without sources, with a caveat naming the reason; the panel shows *RC pages cited (none -- knowledge graph unavailable)* |
| the instructions ask for a follow-up | sources not put in the prompt |
| a lookup command missing, failing or timing out | that lookup says so (or is skipped); any exception inside `lookups.run` returns `[]` |
| `/v1/models` has no `max_model_len` | the budget is `TICKET_MAX_TOKENS` |
| no `/tokenize` | no token breakdown; totals still come from usage |
| the server rejects `json_schema` or `json_object` | the next structured-output strategy |
| run log, metrics, spans cannot be written | ignored: recording is best-effort |
| OpenTelemetry not installed or the collector down | a warning; `otel.export` never raises |
| the model fails | 502 with a UI-ready message and the `request_id`; the failure is recorded too |
| anything unexpected | 500 with a generic message; the trace is in the server log |

`GET /api/health` is **always 200**, even with vLLM down. The code comment
explains why: a 503 is indistinguishable from OOD's own 503 (*nothing is
listening on that port*), so the extension would report a dead job while the
backend was answering fine and waiting for the model. The body's `status`
carries the answer.

## 4.7 Startup and readiness

In an OOD session `script.sh.erb` starts, in order: rc-kg (loopback, CPU,
1-2 minutes to ready), **the backend**, then vLLM (loopback). The backend comes
first so that the extension, which configures itself the moment the card
appears, gets progress instead of several minutes of 502s.

`GET /api/health` reports both servers:

1. `check_health()` for vLLM; `kg_client.health()` for rc-kg: `ok`,
   `loading`, `unreachable`, `incompatible` (API version) or `disabled`.
2. If vLLM did not answer: `status: unreachable` with the reason.
3. Otherwise `status: ok`, the served models, and `model_available`
   (`False` with a detail when the configured name is not among them).
4. Either way, `startup` from `app/startup.py`.

`startup.startup(vllm_reachable, kg_state, kg_detail)` returns
`{ready, model_ready, elapsed_s, vllm, knowledge_graph}`:

- **vLLM** (`vllm_progress`) reads up to 4 MB of `$VLLM_LOG_FILE` and finds
  the last stage whose marker appears. Stages and where they start on the bar:
  *Starting vLLM* 0, *Initializing the engine* 5, *Loading model weights* 10,
  *Compiling the model* 60, *Profiling memory / KV cache* 70, *Capturing CUDA
  graphs* 75, *Starting the API server* 95. Within weights it uses the shard
  counter (`Loading safetensors checkpoint shards: n/m`); within graphs, the
  capture counter, spread over 4 expected passes (`_GRAPH_PASSES`). A
  high-water mark keeps the bar from moving backwards. A traceback, CUDA
  out-of-memory, engine-core failure, *No CUDA GPUs are available* or the line
  `rc-copilot: vLLM exited with status N` (which the job script appends when
  vLLM dies) marks it failed. Markers match vLLM 0.29's log; unknown lines are
  ignored, so another version gives coarser stages, never an error.
- **rc-kg** (`kg_progress`) reads one `<step>.json` per step from
  `$RC_KG_PROGRESS_DIR`: *Creating a private Neo4j*, *Preparing the Python
  runtime*, *Loading the graph snapshot*, *Starting Neo4j*, *Starting the graph
  API*, *Loading the embedding model*, *Connecting to the graph*, *Warming up
  queries*. The percentage is the share of steps done.
- `ready` = vLLM ready and rc-kg ready or disabled. `elapsed_s` counts from the
  backend's import.

The extension can draft as soon as the model is ready; until rc-kg is, drafts
degrade as in 4.6. If vLLM's served name differs from the one the job script
guessed, the script restarts the backend with the real name (which ends
existing sessions, 4.3).

`tests/startup/test_vllm_progress.py` replays a real vLLM 0.29 startup log
line by line through `vllm_progress`.

## 4.8 What each draft leaves behind

| record | where | written by |
|---|---|---|
| run log | `<RUN_LOG_DIR>/<ticket>/<YYYYmmdd-HHMMSS>-<request_id>.json` | `runlog.record` |
| feedback | beside it, `<run>.feedback.jsonl` | `runlog.add_feedback` |
| spans | `traces/$USER/spans/<date>.jsonl`, and an OTLP collector when `OTEL_EXPORTER_OTLP_ENDPOINT` is set | `otel.export` |
| metrics | `traces/$USER/metrics/<date>.jsonl` | `ops_store.append` |
| log lines | stdout (and `LOG_FILE`); sizes, ids, outcomes, never ticket text unless `DEBUG` | `logging` |

`ops_store.root()` is `<repo>/traces/$USER` (or `RC_TRACES_DIR`). The run-log
directory is `RUN_LOG_DIR`, defaulting to `traces/$USER/runs`; `RUN_LOG_DIR=""`
turns run logs off. Directories are created 0700 and files 0600: ticket text
can contain researcher data.

The run log holds the request, the exact prompt messages, the raw output,
the strategy, the draft or error, and (`trace_version` 3) the full trace:
versions, ticket-context stats, retrieval, lookups, playbooks, every model
call, tokens, citations, the draft before and after citations, timings and
the run tree with its feedback. `ticket_folder` takes the ticket number, or
finds one in the text (`SCTASK`, `RITM`, `INC`, `TASK`, `CHG`, `PRB`, `REQ`,
`CS` + 5 or more digits), strips everything but `[A-Za-z0-9_-]` and cuts to
64 characters, because the value comes from a web page and must not escape
the directory.

`POST /api/feedback` finds the run by request id (`*/*-<id>.json`) and appends
the record with an id, the time, `$USER` and `source: user`. When it carries a
`correction`, a second record `edit_similarity` (`difflib` ratio of the
drafted reply against the reply sent; 1 = sent unchanged) is added with
`source: derived`. With run logs off there is nothing to attach to: 404.

> **rc-agent:** run logs default to `rc-agent/traces/$USER/runs`, with the
> traces, metrics, guardrail findings and evals (chapter 17).

## 4.9 Concurrency

One uvicorn process with one asyncio event loop serves every request of the
session. Within a draft:

| work | how it avoids blocking |
|---|---|
| cluster lookups | `asyncio.create_subprocess_exec`, gathered; started as a task before retrieval and awaited after it |
| rc-kg calls | blocking `urllib` in `asyncio.to_thread` (`kg_client._acall`) |
| vLLM calls | the async OpenAI client |
| `/tokenize` counts | blocking `urllib` in `asyncio.to_thread`, gathered |
| the LLM judge | `asyncio.create_task` after the response is built; it does not delay the response |
| playbooks, cleaning, fitting, run log, metrics | synchronous, in the request; small |

The run tree's current parent is a `contextvars.ContextVar`, which tasks and
`to_thread` inherit, so deep code opens child runs without being passed the
trace.

Several drafts can be in flight at once; vLLM batches them. State kept in the
process, and why one process is required:

| state | module |
|---|---|
| the access token and the session signing key | `auth` (a second worker would reject the first one's sessions) |
| the structured-output strategy that last worked | `llm_client._working_strategy` |
| the context window per model | `llm_client._context_window` |
| the progress bar's high-water mark | `startup._high_water` |
| the OpenTelemetry tracer | `otel` |

## 4.10 Settings this chapter uses

From `app/config.py` (pydantic-settings: environment variables of the same
name, case-insensitive, or a `.env` in the working directory). Chapter 14
lists them all.

| setting | default | used for |
|---|---|---|
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | the model server |
| `VLLM_MODEL_NAME` | `meta-llama/Llama-3.1-8B-Instruct` | the model id; the job script sets the real one |
| `VLLM_API_KEY` | unset | sent as `EMPTY` when unset |
| `AUTH_TOKEN` | unset (generated) | the access token |
| `SESSION_HOURS` | 3.0 | session lifetime |
| `AUTH_DISABLED` | false | skip sign-in (local only) |
| `RC_KG_URL`, `RC_KG_API_TOKEN` | unset | rc-kg; unset = no grounding |
| `CLUSTER_LOOKUPS`, `CLUSTER_LOOKUP_TIMEOUT_SECONDS` | true, 5.0 | chapter 6 |
| `PLAYBOOKS` | true | chapter 7 |
| `TICKET_CONTEXT_CLEANING`, `TICKET_MAX_TOKENS` | true, 6000 | chapter 5 |
| `TEMPERATURE`, `MAX_TOKENS`, `REQUEST_TIMEOUT_SECONDS` | 0.2, 1400, 120.0 | generation |
| `USE_PROXY_ENV` | false | proxy bypass for the model host |
| `TRACE_TOKEN_BREAKDOWN` | true | step 11 |
| `ONLINE_EVAL`, `ONLINE_EVAL_SAMPLE_RATE` | true, 1.0 | the background judge |
| `DEBUG` | false | log ticket text (never on shared hosts) |
| `LOG_FILE` | `logs/rc_copilot.log` | `""` = stdout only; the job script sets `""` |
| `APP_PORT` | 8080 | `python -m app.main` only |

Read from the environment directly: `RUN_LOG_DIR`, `RC_TRACES_DIR`,
`RC_TRACES_SPANS`, `OTEL_*`, `VLLM_LOG_FILE`, `RC_KG_PROGRESS_DIR`,
`SLURM_JOB_ID`, `RC_COPILOT_RELEASE`.

## 4.11 The paste-a-ticket page

`backend/web/` (`index.html`, `app.js`, `style.css`; no build step, no
dependencies) is served at `/`. It is the same pipeline without ServiceNow:
paste the access token, paste a ticket, draft, read the checks, give
feedback. It calls the same endpoints. Unlike the extension, it keeps the
session in `localStorage` (`rc_copilot_session`), so a sign-in survives a
reload; the expiry there is for the UI only, the backend enforces it.
`tests/ui/web_feedback_test.py` drives it in Chromium against a real backend,
rc-kg and the stub model.
