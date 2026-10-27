# RC Copilot backend

The FastAPI service behind RC Copilot. It takes ticket text in and returns a
structured draft reply, using a model served by [vLLM](https://docs.vllm.ai)
on Explorer and grounded in the RC website through [rc-kg](../../rc-kg).

Two clients use it: the browser extension in [`../extension`](../extension),
which reads the open ServiceNow ticket and writes the draft into the ticket
unsaved, and the single-page web UI served from [`web/`](web), where you paste
ticket text by hand. The backend itself knows nothing about ServiceNow.

In normal use the OOD app (or `cluster/serve_stack.sbatch`) starts this backend
for you, together with vLLM and rc-kg; see the
[repository README](../../README.md#running-it). This page is for running it
by hand, configuring it, and tuning the prompt.

Every draft is a *starting point*. Nothing is sent anywhere automatically, and
the model can be confidently wrong — read the **Check before sending** list.

---

## What it does

1. Ticket text arrives (short description, description, comments and work
   notes) from the extension or the web UI's paste box, with any instructions.
2. The backend cleans it (`app/ticket_context.py`: notification emails, address
   lists and signatures out; the thread oldest first, labelled RC staff or
   researcher), runs read-only cluster lookups for what it names
   (`app/lookups.py`), and retrieves the most relevant RC website and rc-docs
   sections from rc-kg (when it is running).
3. It fits the ticket to the model's context window, builds the prompt
   (`app/prompts.py`) and calls the vLLM server's OpenAI-compatible
   `/v1/chat/completions` endpoint.
4. You get back a structured draft, with `[S1]`-style citations turned into a
   References block of exact page links:

   | Field | What it is |
   | --- | --- |
   | `problem_summary` | One or two sentences, written for a colleague |
   | `suggested_steps` | Diagnostic/resolution steps for you, most likely cause first |
   | `confidence` + `confidence_label` | 0–1 score; `high` ≥ 0.8, `medium` 0.6–0.79, `low` < 0.6 |
   | `caveats` | What to verify before sending (missing job ID, no error text, …) |
   | `draft_response` | Ready-to-paste reply to the researcher |

5. The draft is checked automatically (see the
   [app README](../README.md#evaluation-of-every-draft)) and recorded to a
   private run log. You edit it and send it yourself.

---

## Setup

Requires Python 3.9+ (developed and tested against 3.11).

```bash
cd backend

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env     # then edit .env — see below
```

Run it:

```bash
uvicorn app.main:app --reload --port 8080
```

Open <http://127.0.0.1:8080> and sign in with the access token printed in the
console at startup (see [Authentication](#authentication)).

`--port 8080` is worth typing: uvicorn's own default is 8000, which is also
vLLM's default, so plain `uvicorn app.main:app --reload` will collide with a
vLLM server (or an SSH tunnel to one) on the same machine. Alternatively run
`python -m app.main`, which reads `APP_PORT` from your `.env`.

The status dot in the top right tells you whether the model server is reachable.

---

## Pointing it at a vLLM instance

Start vLLM on the cluster (on a GPU node, via `srun`/`sbatch`):

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct --host 0.0.0.0 --port 8000
```

Then set in `.env`:

```ini
VLLM_BASE_URL=http://<node>:8000/v1
VLLM_MODEL_NAME=meta-llama/Llama-3.1-8B-Instruct
```

`VLLM_BASE_URL` **must include the trailing `/v1`**. `VLLM_MODEL_NAME` must match
an id from `GET <VLLM_BASE_URL>/models` — usually exactly what you passed to
`vllm serve`. `/api/health` will tell you if it doesn't.

If you're running the app on your laptop and vLLM on a compute node, forward the
port and leave the URL as localhost:

```bash
ssh -N -L 8000:d1234:8000 you@login.explorer.northeastern.edu
```

vLLM needs no auth by default. If you started it with `--api-key`, set
`VLLM_API_KEY` to match.

### A note on proxies

If `HTTP_PROXY`/`HTTPS_PROXY` are set in your shell (they are on some Explorer
nodes), the HTTP client would otherwise try to reach the model server *through*
the site proxy, which rejects internal traffic with a 403. The app therefore adds
the vLLM host to `NO_PROXY` automatically. Set `USE_PROXY_ENV=true` if you
actually need a proxy to reach your endpoint.

---

## Structured output

The backend asks for JSON rather than hoping for it, trying three strategies and
sticking with the first that works:

1. `response_format={"type": "json_schema", ...}` — guided decoding against a
   strict schema. Recent vLLM and the OpenAI API both support this.
2. `response_format={"type": "json_object"}` — for older vLLM builds.
3. Prompt-only, with an explicit "raw JSON, no fences" reminder.

Whatever comes back is parsed (fenced code blocks and stray commentary are
tolerated) and validated against a Pydantic model. If it still doesn't parse you
get a clear error in the UI, not a stack trace. `confidence_label` is computed
server-side from `confidence` so the thresholds are ours, not the model's.

---

## Dev convenience: testing against OpenAI instead

`VLLM_BASE_URL` is just an OpenAI-compatible endpoint, so for local development
without HPC access you can point it at OpenAI:

```ini
VLLM_BASE_URL=https://api.openai.com/v1
VLLM_MODEL_NAME=gpt-4o-mini
VLLM_API_KEY=sk-...
USE_PROXY_ENV=true   # only if you need a proxy to reach the internet
```

**This is for local prompt iteration only.** Do not use it with real ticket text
— that sends researcher data to a third party, which is the thing this whole
design is avoiding. Use synthetic tickets, and switch back to the on-cluster
vLLM endpoint for anything real.

---

## Standard process (the node changes every time)

vLLM lands on a different compute node with every allocation, so nothing should
have a node name typed into it. Don't reuse yesterday's tunnel command — a stale
node name produces "Connection refused", which reads exactly like the server
being down.

The topology that has the fewest moving parts: **vLLM on the cluster, RC Copilot
on your laptop, one tunnel between them.** The backend then listens on your own
`127.0.0.1`, so nothing reaps it, no one else on a shared node can reach it, and
the browser extension's default settings work untouched.

```
laptop                                    cluster
┌──────────────────────────┐              ┌────────────────────┐
│ browser / extension      │              │ serve_glm.sh       │
│   -> 127.0.0.1:8080      │              │   vLLM on d####    │
│ uvicorn (RC Copilot)     │   ssh -L     │   :8000            │
│   -> 127.0.0.1:8000 ─────┼──────────────┼─> d####:8000       │
└──────────────────────────┘              └────────────────────┘
```

**1. Start vLLM** (on a login node):

```bash
cd /projects/rc/projects/ticketmate/ticketmate/cluster
sbatch --partition=gpu-short --time=02:00:00 \
       --gres=gpu:a100:1 --mem=80G --export=ALL,MODEL=qwen7b serve_stack.sbatch
```

(`serve_stack.sbatch` also starts a backend inside the job. For this topology
you ignore it and talk to the vLLM port through the tunnel.)

**2. Ask where it landed.** `vllm_status.sh` reads the node out of Slurm, checks
the API is actually answering, and asks the server for its own model id — then
prints the exact commands with that node filled in:

```bash
./vllm_status.sh -w        # -w waits while the model loads
```

**3. Run the two commands it printed** — the `ssh -N -L ...` on your laptop in
its own terminal, then `uvicorn` in another.

To skip the copy-paste, on the laptop:

```bash
eval "$(ssh you@login.explorer.northeastern.edu \
        /projects/rc/projects/ticketmate/ticketmate/cluster/vllm_status.sh --env)"
uvicorn app.main:app --port 8080
```

`--env` emits only `export VLLM_BASE_URL=...` and `export VLLM_MODEL_NAME=...`.
Real environment variables take precedence over `.env`, so this overrides your
file without editing it.

**When the job ends or you start a new one, re-run step 2.** That is the whole
maintenance burden.

---

## Running it on the cluster

Two things bite on shared nodes.

**Don't use `--reload` on a login node.** `.venv` lives inside `backend/`, so
uvicorn's file watcher recursively scans thousands of files on `/projects` at
startup. That resource spike gets the process `Killed` by the node's enforcement
daemon, usually seconds after `Application startup complete`. Either drop the
flag, or scope the watcher so it never sees `.venv`:

```bash
uvicorn app.main:app --port 18080 --reload --reload-dir app --reload-dir web
```

**Ports are host-wide.** `Address already in use` on port 8080 usually means
another user on that login node has it, not you. Pick something uncommon
(`18080`, `19080`) and use the same number consistently. `ss -ltnp | grep <port>`
shows a PID only if the process is yours.

Better than either: run on a compute node, which is where vLLM lives anyway, so
`VLLM_BASE_URL` can stay on localhost with no second tunnel.

```bash
srun --pty --time=4:00:00 --mem=4G bash
hostname                       # tunnel to this node, not the login node
uvicorn app.main:app --port 18080
```

A note on `--host`: uvicorn binds `127.0.0.1` by default. Keep it that way. On a
shared node "localhost" still means every user logged into that host. The
access token keeps them out of the API, but never add `--host 0.0.0.0` on a
login node, and never run with `AUTH_DISABLED=true` on a shared one.

---

## Browser extension

Lives in [`../extension`](../extension), not here.
It reads the open ServiceNow ticket, calls this backend through the Open
OnDemand proxy, and writes the draft into the ticket's work notes (or the field
chosen in its panel). Details: [../docs/EXTENSION.md](../docs/EXTENSION.md).

Note the deployment difference: the current extension expects this backend to be
reachable **through OOD**, which means it runs on a compute node bound to
`0.0.0.0` and requires the access token described under Authentication. The
tunnel-to-localhost setup above is still the right choice for local prompt
iteration.

---

## Authentication

Every `/api/*` route except `/api/health` needs a session. You get one by
posting the **access token** to `/api/login`; the response is a signed session
token valid for `SESSION_HOURS`. The web UI and the extension do this for you.

- Unset `AUTH_TOKEN` (the default): a random token is minted per run and
  printed to the console only. Under the OOD app it is set to the password OOD
  generated for the session, which the extension reads from the session card.
- `cluster/serve_stack.sbatch` writes it to `~/.rc_copilot/token` (mode 600).
- Don't put `AUTH_TOKEN` in `.env` on the cluster: `/projects` is
  group-readable.

---

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /` | The single-page web UI (from `web/`) |
| `POST /api/login` | `{"access_token": "..."}` → `{"session_token", "expires_at"}` |
| `POST /api/draft` | `{"ticket_text", "extra_instructions"?, "ticket_number"?}` → `DraftResponse`, including `request_id`, references, the cluster lookups that ran, what the ticket was cleaned to (`context`), checks and token usage |
| `POST /api/feedback` | `{"request_id", "key", "score"?: -1..1, "value"?, "comment"?, "correction"?}` — stored beside the draft's run log |
| `GET /api/health` | Model server, knowledge graph (`knowledge_graph`) and `startup` progress. Always `200`, so it can't be confused with OOD's own 503; read the body |
| `GET /docs` | FastAPI's generated API docs |

Errors come back as `{"error": "...", "detail": "..."}` — `401` without a valid
session, `502` when the model server can't be used, `422` for an empty or
oversized ticket (200,000 characters of raw page text max; it is cleaned and fitted to the model).

---

## Configuration

All settings live in `.env` (see `.env.example` for the annotated version).

| Variable | Default | Notes |
| --- | --- | --- |
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | Include the trailing `/v1` |
| `VLLM_MODEL_NAME` | `meta-llama/Llama-3.1-8B-Instruct` | Must match `/v1/models` |
| `VLLM_API_KEY` | *(empty)* | Only if the server requires auth |
| `AUTH_TOKEN` | *(minted per run)* | See [Authentication](#authentication); leave unset on the cluster |
| `SESSION_HOURS` | `3.0` | How long a sign-in lasts |
| `AUTH_DISABLED` | `false` | Loopback on your own machine only |
| `RC_KG_URL` | *(unset)* | rc-kg API, e.g. `http://127.0.0.1:8090`. Unset = drafts are not grounded |
| `RC_KG_API_TOKEN` | *(unset)* | Bearer token for rc-kg; the session job generates it |
| `RC_KG_K` | `6` | Website sections put in the prompt per ticket |
| `RC_KG_TIMEOUT_SECONDS` | `15` | Only matters if rc-kg is wedged |
| `TICKET_CONTEXT_CLEANING` | `true` | Strip notification emails, address lists, signatures and quotes; order the thread oldest first ([app README](../README.md#ticket-context-what-the-model-actually-reads)) |
| `TICKET_MAX_TOKENS` | `6000` | Most of the prompt the ticket may take; it also always fits the model's context window |
| `CLUSTER_LOOKUPS` | `true` | Read-only `id` / `sacct` / `getent group` / `scontrol show node` / `sinfo` / `squeue` / `sshare` / `stat` / `ldconfig -p` lookups for what the ticket names ([app README](../README.md#cluster-lookups-read-only)) |
| `PLAYBOOKS` | `true` | RC workflow runbooks (`app/playbooks/*.md`) put in the prompt when a ticket is one of their requests ([app README](../README.md#rc-workflow-playbooks)) |
| `CLUSTER_LOOKUP_TIMEOUT_SECONDS` | `5` | Per command |
| `APP_PORT` | `8080` | Used by `python -m app.main` |
| `TEMPERATURE` | `0.2` | Low keeps the JSON stable |
| `MAX_TOKENS` | `1400` | Raise if drafts get truncated |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Cluster queues can be slow |
| `USE_PROXY_ENV` | `false` | See the proxy note above |
| `TRACE_TOKEN_BREAKDOWN` | `true` | Count prompt tokens per part (system / ticket / sources / instructions) with vLLM's `/tokenize` |
| `ONLINE_EVAL` | `true` | The serving model grades each draft in the background |
| `ONLINE_EVAL_SAMPLE_RATE` | `1.0` | Share of drafts graded |
| `RUN_LOG_DIR` | `/scratch/$USER/rc_copilot_runs` | One private JSON trace per draft; empty = off. Read by `app/runlog.py` directly |
| `DEBUG` | `false` | **Logs full ticket text** — see below |
| `LOG_FILE` | `logs/rc_copilot.log` | Empty value = stdout only |

Relative paths resolve against the directory you launch uvicorn from.

---

## Logging and privacy

Each request logs **metadata only** — timestamp, request id, input length,
confidence, label, step/caveat counts, latency, model:

```
2026-09-15 04:08:57 INFO rc_copilot: draft ok | id=6041643f input_chars=23 \
  confidence=0.86 label=high steps=3 caveats=1 latency_ms=15 model=stub-model
```

Ticket text is **not** written to the log. Setting `DEBUG=true` logs the full
ticket text and model output; it's for debugging a prompt on your own machine
and prints a warning at startup. Don't set it on a shared host. Logs go to
stdout and `LOG_FILE`.

The **run log** is separate and does hold ticket text: one JSON trace per draft
under `RUN_LOG_DIR`, in a mode-700 directory with mode-600 files, so only you
can read it. That is what `cluster/traces.py` reads. There is no database.

---

## Tuning the prompt

`app/prompts.py` is the file you'll actually iterate on. `SYSTEM_PROMPT`
holds the role, the JSON contract, the confidence rubric, and the rules about
what the draft must not do (invent partitions or paths, promise timelines or
quota increases). With `--reload`, saving it restarts the server.

If you change the *shape* of the output, update `LLMDraft` and
`DRAFT_JSON_SCHEMA` in `app/models.py`, the renderer in `web/app.js`, and the
panel in `../extension/content.js` to match. Every prompt template is hashed
into the trace's `prompt_version`, so
`../cluster/traces.py stats --by prompt_version` compares revisions.

---

## Layout

```
backend/
  app/
    main.py         FastAPI app: API routes + serves web/
    config.py       Settings from env / .env
    auth.py         Access token, signed sessions
    llm_client.py   vLLM calls, structured-output fallback, JSON extraction, token counts
    models.py       Pydantic models + the JSON schema sent to the server
    prompts.py      Prompt templates  <- tune here
    kg_client.py    rc-kg retrieval and citation processing
    lookups.py      Read-only cluster lookups (id, sacct, getent, scontrol, sinfo, squeue, sshare, stat, ldconfig)
    playbooks.py    RC workflow playbooks: match a ticket to a runbook in playbooks/*.md
    ticket_context.py  Cleans the scraped ticket and fits it to the context window
    evaluators.py   Per-draft checks and the background LLM judge
    runlog.py       The private per-draft run log
    tracing.py      Run trees (LangSmith-style) written into the run log
    startup.py      Startup progress from vLLM's log and rc-kg's checklist
  web/
    index.html      Sign-in + paste box, button, results panel, feedback form
    app.js          fetch() + rendering, no build step
    style.css
  .env.example
  requirements.txt
```

The browser extension lives one level up, in
[`../extension`](../extension).

---

## Scope

Retrieval is rc-kg's job, over HTTP; this backend holds no index or database.
Drafts are stored only in your own run log.

This backend has no knowledge of ServiceNow: it takes text in and returns a
draft. The reading and writing of ticket pages lives entirely in
[`../extension`](../extension), which is also
where the "nothing is saved automatically" guarantee is enforced — the draft is
placed in the work notes field unsaved, for a human to review and submit.
