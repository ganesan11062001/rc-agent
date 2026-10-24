# RC Copilot (v0)

An internal tool that helps Research Computing staff draft replies to ServiceNow
tickets using a local LLM served by [vLLM](https://docs.vllm.ai) on Discovery.

**Manual copy-paste only.** There is no ServiceNow integration, no scraping, and
no write-back — RC policy doesn't give us API access, so an RC member pastes the
ticket text in, reads the draft, and copies it out. The point of v0 is to find
out whether the prompt produces useful drafts and whether the vLLM connection
holds up end to end.

Every draft is a *starting point*. Nothing is sent anywhere automatically, and
the model can be confidently wrong — read the **Check before sending** list.

---

## What it does

1. You paste ticket text (short description, description, work notes — whatever
   you copied) into a box.
2. The backend builds a prompt and calls the vLLM server's OpenAI-compatible
   `/v1/chat/completions` endpoint.
3. You get back a structured draft:

   | Field | What it is |
   | --- | --- |
   | `problem_summary` | One or two sentences, written for a colleague |
   | `suggested_steps` | Diagnostic/resolution steps for you, most likely cause first |
   | `confidence` + `confidence_label` | 0–1 score; `high` ≥ 0.8, `medium` 0.6–0.79, `low` < 0.6 |
   | `caveats` | What to verify before sending (missing job ID, no error text, …) |
   | `draft_response` | Ready-to-paste reply to the researcher |

4. You edit the draft in the browser and hit **Copy to clipboard**.

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

Open <http://127.0.0.1:8080>.

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
ssh -N -L 8000:d1234:8000 you@login.discovery.neu.edu
```

vLLM needs no auth by default. If you started it with `--api-key`, set
`VLLM_API_KEY` to match.

### A note on proxies

If `HTTP_PROXY`/`HTTPS_PROXY` are set in your shell (they are on some Discovery
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
cd /projects/rc/projects/ticket_mate/cluster
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
        /projects/rc/projects/ticket_mate/vllm_status.sh --env)"
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

**Don't use `--reload` on a login node.** `.venv` lives inside `rc-copilot`, so
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
shared node "localhost" still means every user logged into that host, and this
app has no auth, so ticket text in a running instance is readable by them. Never
add `--host 0.0.0.0` on a login node.

---

## Browser extension

Lives in [`../extension`](../extension), not here.
It reads the open ServiceNow ticket, calls this backend through the Open
OnDemand proxy, and writes the draft into the ticket's work notes.

There used to be a second, side-panel-only extension in `browser-extension/`
that framed `http://127.0.0.1:8080` over an SSH tunnel. It was superseded and
has been deleted — if you still have it loaded in Chrome or unzipped on your
laptop, remove it. It uses the same icon as the current one, so having both
installed is genuinely confusing.

Note the deployment difference: the current extension expects this backend to be
reachable **through OOD**, which means it runs on a compute node bound to
`0.0.0.0` and requires the access token described under Authentication. The
tunnel-to-localhost setup below is still the right choice for local prompt
iteration.

---

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /` | The single-page frontend |
| `POST /api/draft` | `{"ticket_text": "..."}` → `DraftResponse` |
| `GET /api/health` | Pings the vLLM server; `200` reachable, `503` not |
| `GET /docs` | FastAPI's generated API docs |

Errors come back as `{"error": "...", "detail": "..."}` — `502` when the model
server can't be used, `422` for an empty paste.

---

## Configuration

All settings live in `.env` (see `.env.example` for the annotated version).

| Variable | Default | Notes |
| --- | --- | --- |
| `VLLM_BASE_URL` | `http://localhost:8000/v1` | Include the trailing `/v1` |
| `VLLM_MODEL_NAME` | `meta-llama/Llama-3.1-8B-Instruct` | Must match `/v1/models` |
| `VLLM_API_KEY` | *(empty)* | Only if the server requires auth |
| `APP_PORT` | `8080` | Used by `python -m app.main` |
| `TEMPERATURE` | `0.2` | Low keeps the JSON stable |
| `MAX_TOKENS` | `1400` | Raise if drafts get truncated |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Cluster queues can be slow |
| `USE_PROXY_ENV` | `false` | See the proxy note above |
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

Ticket text is **not** logged. Setting `DEBUG=true` logs the full ticket text and
model output; it's for debugging a prompt on your own machine and prints a
warning at startup. Don't set it on a shared host. There is no database — logs go
to stdout and `LOG_FILE`.

---

## Tuning the prompt

`backend/prompts.py` is the file you'll actually iterate on. `SYSTEM_PROMPT`
holds the role, the JSON contract, the confidence rubric, and the rules about
what the draft must not do (invent partitions or paths, promise timelines or
quota increases). With `--reload`, saving it restarts the server.

If you change the *shape* of the output, update `LLMDraft` and
`DRAFT_JSON_SCHEMA` in `backend/models.py` and the renderer in
`frontend/app.js` to match.

---

## Layout

```
rc-copilot/
  backend/
    main.py         FastAPI app: API routes + serves the frontend
    config.py       Settings from env / .env
    llm_client.py   vLLM calls, structured-output fallback, JSON extraction
    models.py       Pydantic models + the JSON schema sent to the server
    prompts.py      Prompt templates  <- tune here
  frontend/
    index.html      Sign-in + paste box, button, results panel
    app.js          fetch() + rendering, no build step
    style.css
  .env.example
  requirements.txt
```

The browser extension lives one level up, in
[`../extension`](../extension).

---

## Scope

No vector store or retrieval, and no database — drafts are not stored anywhere.

This backend has no knowledge of ServiceNow: it takes text in and returns a
draft. The reading and writing of ticket pages lives entirely in
[`../extension`](../extension), which is also
where the "nothing is saved automatically" guarantee is enforced — the draft is
placed in the work notes field unsaved, for a human to review and submit.
