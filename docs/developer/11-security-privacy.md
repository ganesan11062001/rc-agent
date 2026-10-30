---
name: 11. Security and privacy
route: /security
menu: Operations
---

# 11. Security and privacy

RC Copilot reads ServiceNow tickets, which hold researchers' names, emails,
usernames, job details and sometimes unpublished work. This chapter says what
we protect, from whom, and lists every control in the code, with where it
lives. When you change any of the files named here, re-read the section first.

## 11.1 The threat model

### What we protect

| asset | where it exists | why it matters |
|---|---|---|
| Ticket text (and the activity stream) | the extension, the backend's memory, the prompt, run logs and traces | researcher data; must not leave the university |
| The backend's access token | the OOD session card, the backend's memory, the extension's storage | anyone with it can draft with your job and read the drafts |
| Session tokens | the backend signs them; the extension keeps them | the same, until they expire |
| rc-kg's API token, Neo4j's password | the job's environment | someone else on the node could query or change your graph |
| RC's procedures (playbooks) | the private playbook directory (`PLAYBOOK_DIR`) | internal; the repositories are public |
| The cluster | the engineer's own account | lookups run as the engineer, on text a researcher wrote |

### Who we protect it from

| adversary | what they can do | consequence |
|---|---|---|
| Any cluster user | reach any port on any compute node that listens on `0.0.0.0`; read group-readable files on `/projects` | the backend must authenticate; tokens never go to `/projects` |
| Any user on the same GPU node | reach `127.0.0.1` too: loopback is shared by everyone on a node | rc-kg needs a token even on loopback; Neo4j gets a random password |
| A researcher writing a ticket | put anything in the text: shell syntax, fake usernames, prompt injection | the model never chooses a command; every argument is validated in code |
| The model itself | write a plausible but wrong link, claim an action was taken | URLs it writes are removed unless retrieved; nothing is sent or saved by the app |
| A web page in the browser | try to point the extension at its own endpoint | the extension only accepts Northeastern hosts |
| Anyone reading the public repositories | read every committed file | no real ticket, person or internal procedure is ever committed |

### Hard rule: ticket text stays at Northeastern

There is no hosted model API anywhere in the product. The model (vLLM), the
knowledge graph (rc-kg) and the lookups all run inside the RC engineer's own
Slurm job. `backend/README.md` documents pointing `VLLM_BASE_URL` at a hosted
API for local prompt work; it says, and we repeat: **only with synthetic
tickets.**

## 11.2 What listens where

One Slurm job runs everything (chapter 2). Only the backend is reachable from
the network.

| process | address | who can reach it | credential | set up in |
|---|---|---|---|---|
| backend (FastAPI) | `0.0.0.0:$port` | every cluster user, and OOD's node proxy | OOD SSO in front; access token -> signed session | `script.sh.erb` (`--host 0.0.0.0`) |
| vLLM | `127.0.0.1:$VLLM_PORT` | every user on the node | none | `script.sh.erb` passes `VLLM_HOST=127.0.0.1` to `serve_glm.sh` |
| rc-kg API | `127.0.0.1:$RC_KG_PORT` | every user on the node | bearer `RC_KG_API_TOKEN`, random per session | `script.sh.erb`, `serve_kg.sh` (`HOST=127.0.0.1`) |
| Neo4j | `127.0.0.1:<free port>`, Bolt only | every user on the node | user `neo4j`, random password per instance | `_kg_instance.sh` (`kg_instance_init`) |

Ports are never hard-coded in the OOD app: `before.sh.erb` takes `port`,
`VLLM_PORT` and `RC_KG_PORT` from OOD's `find_port`; Neo4j's Bolt port comes
from `kg_free_port` (a port the kernel says is free).

## 11.3 Backend authentication

Two layers stand in front of `/api/draft` and `/api/feedback`.

### Layer 1: Open OnDemand

Members reach the backend only through OOD's node proxy,
`https://ood.explorer.northeastern.edu/rnode/<node>/<port>/`, which requires
the university SSO login. `extension/background.js` sends every request with
`credentials: "include"`, so OOD's SSO cookie goes with it; a 401 or 403 from
OOD is reported as "Your OOD session has probably expired".

OOD is not enough on its own: the backend listens on `0.0.0.0`, so any
cluster user can connect to the port directly, without OOD. Hence layer 2.

### Layer 2: access token and signed sessions (`backend/app/auth.py`)

| step | what happens | code |
|---|---|---|
| Token created | Under OOD, `before.sh.erb` sets `password="$(create_passwd)"`; `script.sh.erb` exports it as `AUTH_TOKEN`. OOD shows it only on the job owner's session card | `before.sh.erb`, `script.sh.erb` |
| ...or minted | With `AUTH_TOKEN` unset, the backend makes one with `secrets.token_urlsafe(24)` and prints it in the startup banner | `auth.access_token` |
| ...or written to `$HOME` | `serve_stack.sbatch` writes `~/.rc_copilot/token` (directory 0700, file 0600, `umask 077`), never to `/projects` | `serve_stack.sbatch` |
| Sign in | `POST /api/login {"access_token"}`; compared with `hmac.compare_digest` (constant time); a wrong token is a 401 and a `failed sign-in attempt` log line | `main.login`, `auth.check_access_token` |
| Session issued | `"<expiry>.<signature>"`: the Unix expiry, and the first 32 hex characters of HMAC-SHA256 over it | `auth.mint_session` |
| Signing key | `secrets.token_urlsafe(32)`, in memory only, made on first use; restarting the backend invalidates every session | `auth._key` |
| Session checked | the signature is checked first, then the expiry it carries; a forged expiry fails the signature | `auth.verify_session` |
| Guard | `require_session` is a FastAPI dependency on `/api/draft` and `/api/feedback`; anything else is a 401 | `auth.require_session` |
| Lifetime | `SESSION_HOURS`, default 3.0; the OOD app sets it to the hours the member asked for, so a sign-in never outlives the job | `config.py`, `script.sh.erb` |

`/api/login`, `/api/health` and the static web page are not guarded.
`/api/health` reveals the model name, the vLLM URL and startup progress, but
no ticket data.

**The token is never logged.** The startup banner is `print`ed, not logged,
because `LOG_FILE` could sit on group-readable `/projects` (`main.lifespan`).
When `AUTH_TOKEN` was supplied, the banner says so and does *not* print it,
because under Slurm stdout becomes a job log (`auth.startup_banner`). The OOD
app also sets `LOG_FILE=""` (stdout only).

**`AUTH_DISABLED=true`** turns layer 2 off. It is only for a backend bound to
`127.0.0.1` on your own machine; every startup then prints a warning banner.
Never set it on the cluster.

### In the extension

| what | where it is kept | why |
|---|---|---|
| backend URL and access token | `chrome.storage.local` (`oodConfig`) | written by `ood-connect.js` from the session card |
| session token | `chrome.storage.session` | memory only, cleared when the browser closes |

`background.js` `saveConfig` refuses any URL whose host is not
`northeastern.edu` or `neu.edu` (or a subdomain), so a page on another site
cannot point the extension at an endpoint of its choosing. The manifest's
`host_permissions` cover the same hosts only. The trust boundary for
auto-configuration is the session card: only the job's owner can see it, the
same boundary OOD uses for Jupyter and RStudio passwords.

## 11.4 rc-kg and Neo4j credentials

### rc-kg API token

- `script.sh.erb` generates `RC_KG_API_TOKEN` with `secrets.token_urlsafe(24)`
  for each session and exports it, so both the backend (client) and
  `serve_kg.sh` (server) get it through the environment only.
- `rc-kg/api/app.py` `require_token` compares the `Authorization: Bearer`
  value with `hmac.compare_digest`; a mismatch is a 401. Every endpoint except
  `GET /health` depends on it.
- With no token configured the check is skipped (local development).
  `serve_kg.sh` then logs `WARNING: RC_KG_API_TOKEN unset; the API is open to
  every user on <node>`.
- rc-kg's request log carries one line per request (id, timings, counts) and
  **never the query**: ticket text stays in the caller's run log
  (`rc_kg.requests` logger in `api/app.py`).

### Neo4j password

`_kg_instance.sh` gives every caller (a session, a build, a test) its own
Neo4j:

- an instance directory on node-local disk (`$RC_KG_TMP`, else `$TMPDIR`,
  else `/tmp`), named `rc_kg_<user>_<label>_XXXX`, mode 0700;
- a generated `neo4j.conf`: `server.default_listen_address=127.0.0.1`, Bolt
  on a free loopback port, `server.http.enabled=false`,
  `server.https.enabled=false`, `dbms.security.auth_enabled=true`;
- a fresh password from `secrets.token_urlsafe(18)`, set with
  `neo4j-admin dbms set-initial-password`, and exported only as
  `NEO4J_PASSWORD` to the processes that need it -- never written to
  `/projects`;
- on exit the directory is deleted; a watchdog deletes it if the owning
  script is killed outright; the next start on the node sweeps any stale
  directory of the same user (`kg_sweep_stale`). Only the caller's own
  directories are ever touched.

The single-developer path, `scripts/start_neo4j.sh`, is different: it writes
the password into `rc-kg/.env` (mode 0600) and `$RC_KG_DATA_DIR/password`
(0600). `.env` is git-ignored. Do not use that path from a Slurm job.

### `POST /admin/swap`

The graph refresh loop (chapter 9, and 13.5) switches a running API to a newer
graph through `POST /admin/swap {uri, user, password, manifest}`. Its controls:

| control | code |
|---|---|
| needs the bearer token like every other endpoint | `dependencies=[Depends(require_token)]` |
| accepts only a Neo4j on this host's loopback: the URI must fully match `bolt://127\.0\.0\.1:\d{2,5}`, else 400 | `swap_endpoint` |
| the new graph is checked (connects, has embedded sections) before it replaces the old driver; a bad graph is a 409 and nothing changes | `graph/connection.swap` |
| the new instance's password reaches `refresh_kg.sh` through a `.creds` file in the 0700 instance directory, mode 0600, deleted as soon as it is read | `refresh_kg.sh` `switch_to_published` |

## 11.5 Cluster lookups are read-only

Lookups run as the engineer, inside their job, on text a researcher wrote.
Chapter 6 explains them; the security rules, all in `backend/app/lookups.py`:

| rule | how |
|---|---|
| A fixed allow-list of programs | `id`, `getent group`, `sacct`, `scontrol show node`, `sinfo`, `squeue`, `sshare`, `stat`, `ldconfig -p`, plus reading the group.xtra file. Nothing that changes data is reachable from the module |
| No shell | `asyncio.create_subprocess_exec` with an argv list; `;`, `$( )`, backticks and quotes are just characters |
| Every argument fully matches a strict pattern | `USERNAME`, `JOB_ID`, `GROUP`, `NODE`, `PARTITION`, `PROJECT`, `LIBRARY` (`re.fullmatch`) or it is dropped |
| Email addresses are never usernames | the candidate pattern excludes `@` |
| A minimal environment | `_env()` passes only `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `SLURM_CONF`, `TZ`: the backend's tokens never reach a child process |
| Bounded | at most 4 users, 3 jobs, 2 groups, 3 nodes, 2 partitions, 2 projects, 3 libraries; a timeout per command (`CLUSTER_LOOKUP_TIMEOUT_SECONDS`, 5 s); output capped at 2,000 characters |
| Off switch | `CLUSTER_LOOKUPS=false` |

`tests/lookups/test_lookups.py` proves it: fake programs on `PATH` record
every argv, and injection text in a ticket runs nothing (chapter 12).

> **rc-agent:** agents can ask for lookups through tools
> (`backend/app/agents/tools.py`). Each tool's arguments are a Pydantic model
> with `extra="forbid"` and the same patterns, anchored; the model's arguments
> are validated before anything runs. Each lookup function also re-checks its
> own argument (`lookups._invalid`), so a caller that skipped validation still
> cannot pass anything else: the refusal is recorded as `(not run)`. No tool
> changes anything. The contract test (scenario F) proves a hostile job id is
> refused and runs nothing.

## 11.6 The model never acts

| what the model might do | what stops it | code |
|---|---|---|
| Write a link | It cites `[S1]`..`[S6]`; rc-kg turns markers into the retrieved section's exact URL, then removes every other URL in the text (a made-up fragment on a retrieved page is cut back to the page). The draft gets a caveat "Removed N link(s)..." | `rc-kg/retrieval/citations.py`, `kg_client.apply_citations` |
| Link to a playbook's page | Links written in a matched runbook are added to the allowed URLs | `playbooks.Match.urls` |
| Run a command | It never chooses one; lookups are chosen by patterns in code | `lookups.find_candidates` |
| Follow a procedure that changes something | Playbook steps are text for the RC engineer; nothing in `playbooks.py` runs a command | `playbooks.py` |
| Send or save the reply | Nothing in the product sends, saves or submits | see below |

**Nothing is saved automatically.** `content.js` writes the draft into the
chosen ServiceNow field (work notes by default) by setting the value and
dispatching `input`/`change` events, so ServiceNow's own unsaved-changes
tracker sees it. It never clicks **Update**. The panel says "nothing is saved
until you click Update in ServiceNow"; choosing *Additional comments* (which
the researcher sees) shows a warning ("one wrong Update sends it", class
`rc-danger`). `saveConfig` never changes the
target field silently.

> **rc-agent:** output and input guardrails add checks on top of these:
> control tokens and prompt-injection wording in the ticket; credentials
> redacted from the draft; destructive commands and leaked work notes sent
> back to the writer; unsourced timelines flagged. See
> [chapter 16](16-guardrails-context.md).

## 11.7 Data at rest: run logs and traces

Every draft is recorded so it can be explained later (chapter 10). That
record holds ticket text, so it is protected like the ticket.

### Where

| record | default path | override | code |
|---|---|---|---|
| run log, one per draft | `<repo>/traces/$USER/runs/<ticket>/<YYYYmmdd-HHMMSS>-<request_id>.json` | `RUN_LOG_DIR` (`""` turns recording off) | `runlog.py` |
| feedback on a draft | beside it: `<...>.feedback.jsonl` | follows the run log | `runlog.add_feedback` |
| OpenTelemetry spans | `<repo>/traces/$USER/spans/YYYY-MM-DD.jsonl` | `RC_TRACES_DIR`; `RC_TRACES_SPANS=off` | `otel.py`, `ops_store.py` |
| per-draft metrics | `<repo>/traces/$USER/metrics/YYYY-MM-DD.jsonl` | `RC_TRACES_DIR` | `main.py`, `ops_store.py` |
| backend log | stdout (the OOD app sets `LOG_FILE=""`) | `LOG_FILE` | `main.configure_logging` |

OOD sessions, batch jobs and local runs all use these defaults
(`script.sh.erb` no longer sets `RUN_LOG_DIR`). A member who cannot write the
repository gets the same layout under `/scratch/$USER/rc_copilot_traces`, also 0700/0600;
`/scratch` is purged monthly. `traces/` itself is mode 755 and owned by the
maintainer; each `traces/$USER/` is 0700, so no one reads another's.

### What a run log contains

| field | contents |
|---|---|
| `input` | the full ticket text, the engineer's instructions, character count |
| `prompt` | every message sent to the model |
| `raw_response`, `draft`, `error` | what came back, parsed |
| `request_id`, `ticket_number`, `at`, `node`, `slurm_job_id`, `model`, `latency_ms`, `strategy` | identification |
| `trace` (`trace_version` 3) | retrieval (sources with text), every model call with tokens, citation processing, timings, versions, and the run tree with its feedback |

Metrics rows carry no ticket text (`main.py`: "no ticket text"). Spans carry
no ticket text unless `OTEL_EXPORT_CONTENT=true` (`otel._content`).
`cluster/traces.py export` writes one CSV row per draft, with no ticket text.

### Permissions

| object | mode | code |
|---|---|---|
| `traces/$USER/` and each `<kind>/` under it | 0700 | `ops_store.path` |
| the run-log directory and each ticket folder | 0700 | `runlog.run_dir`, `runlog.record` |
| every run log, feedback, spans and metrics file | 0600, set at `os.open` | `runlog.record`, `ops_store.append` |
| each eval report directory | 0700 | `ops_store.new_eval_dir` |
| `traces.py` feedback lines and CSV exports | 0600 | `cluster/traces.py` |
| rc-kg's startup progress directory | 0700 | `script.sh.erb` (`mkdir -p -m 700`) |

The ticket folder name comes from a web page, so it is reduced to
`[A-Za-z0-9_-]`, upper-cased and cut to 64 characters; it can never escape the
run-log directory (`runlog.ticket_folder`). A request id must be 8 hex
characters before it is used in a file lookup (`runlog.find_run`).

Recording is best-effort: a full disk or a permission error never breaks a
draft.

### `DEBUG`

`DEBUG=true` writes the full ticket text and the full response to the log
(`main.draft`), and the backend warns about it at every start. Only for local
debugging with synthetic tickets.

## 11.8 What git never sees

The repositories are public. These `.gitignore` entries keep data and secrets
out:

| file | entries | what it keeps out |
|---|---|---|
| `<repo>/.gitignore` | `/traces/`, `/.uv/`, `/.cache/`, `/rc-copilot/` | every trace and run log; toolchain, model weights; published releases |
| `ticketmate/.gitignore` | `.env`, `.env.local`, `*.token`, `.rc_copilot/` | secrets |
| | `rc_copilot_captures/`, `captures/` | captured ticket data |
| | `logs/`, `*.log`, `slurm-*.out` | job logs (`tests/logs/.gitkeep` is kept so Slurm can write there) |
| `rc-kg/.gitignore` | `.env`, `vendor/`, `data/graph*/`, `data/dumps/`, `data/*.json` | Neo4j's dev password, binaries, graph snapshots and reports |

## 11.9 Private playbooks

RC's real procedures are internal. The repositories ship one generic,
made-up example, `backend/app/playbooks/example-procedure.md`. The real ones
live in the private playbook directory (`PLAYBOOK_DIR`), outside both
repositories:

- `playbooks.playbook_dir()` returns `$PLAYBOOK_DIR`, else the bundled example
  directory.
- `cluster/env.sh` sets `PLAYBOOK_DIR` when the private directory exists, so
  every session on Explorer uses it.
- The playbook tests and the rc-agent evals print
  `SKIP: RC's playbooks are not available here (set PLAYBOOK_DIR)` and exit 0
  without it.

Never copy a playbook's body, or a value from one, into the repository, a test
or this guide.

## 11.10 Keeping the public repositories clean

What the code already does, and what to keep doing:

- **Tests use synthetic tickets.** `test_ticket_context.py` builds a ticket
  for "Ada Lovelace", `INC0000042`, job `4812345`, with team addresses like
  `staff0@northeastern.edu`. Do the same in every new test.
- **Reviewed tickets are reworded before they become cases.**
  `rc-kg/eval/ticket_cases.yaml` and (rc-agent) `evals/groundtruth.yaml` say so
  in their headers: no names, usernames, NUIDs or IDs of real people are kept.
  Recipe 13.7 gives the steps.
- **Never copy from a run log into the repository.** Run logs hold real
  ticket text; read them where they are.
- **Reports hold aggregates.** `rc-kg/eval/variants.py` and
  `tests/agent/replay_eval.py` write ticket numbers, routes and scores, not
  text.
- **Before committing,** read your diff for ticket numbers, usernames, email
  addresses and pasted ticket text.

## 11.11 Known limits

These are true of the code today; know them before you rely on a control.

| limit | detail |
|---|---|
| vLLM has no credential | It listens on loopback, but any user on the same node can send it prompts. It stores nothing, so this exposes GPU time, not ticket data |
| `serve_glm.sh` alone binds `0.0.0.0` | `--host "${VLLM_HOST:-0.0.0.0}"`. The OOD app and `serve_stack.sbatch` pass `127.0.0.1`; if you run it by hand, pass `VLLM_HOST=127.0.0.1` |
| rc-kg without a token is open | to every user on the node; `serve_kg.sh` warns |
| The access token is in `chrome.storage.local` | It persists in the browser profile until the next session card replaces it. It is useless once the job ends |
| `/api/health` is unauthenticated | It names the model and the vLLM URL |
