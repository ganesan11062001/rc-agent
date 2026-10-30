---
name: 12. Testing
route: /testing
menu: Operations
---

# 12. Testing

> **New:** Tests for the sign-off, request-form extraction, the judge gate and readable traces: chapter 19.6.

Every test here runs without a GPU except the inference test. Most need
nothing at all; a few start a real rc-kg from the published snapshot and a
stub model. This chapter lists each suite, what it proves, how to run it, what
a pass looks like, and when to run it.

## 12.1 The suites at a glance

Paths are relative to `ticketmate/` unless they start with `rc-kg/`.

| suite | proves | needs | time |
|---|---|---|---|
| `tests/ticket_context/test_ticket_context.py` | cleaning keeps every message and drops the noise; fitting to a budget | nothing | seconds |
| `tests/lookups/test_lookups.py` | only allow-listed, read-only commands run; injection runs nothing; timeouts | nothing | seconds |
| `tests/playbooks/test_playbooks.py` | every playbook parses, routes the right tickets, extracts values | `PLAYBOOK_DIR` (else SKIP) | seconds |
| `tests/startup/test_vllm_progress.py` | the vLLM log parser, on a real vLLM 0.29 log | nothing | seconds |
| `tests/startup/run_slow_start.sh` | startup progress is reported throughout a slow start | rc-kg snapshot | ~1 min |
| `tests/ui/run.sh` | the web page and the extension's result panel, in headless Chromium | Playwright, rc-kg snapshot | ~1-2 min |
| `tests/ui/extension_form_test.py` | the extension reads catalog request forms | Playwright | seconds |
| `tests/ui/extension_startup_test.py` | the panel shows startup progress | Playwright, a recorded timeline | seconds |
| `extension/check.py` | the extension's static checks | backend venv (esprima) | seconds |
| `tests/rc_kg/run_contract.sh` | the whole draft path against a real rc-kg and a stub model | rc-kg snapshot | ~1-5 min |
| `tests/run_inference_test.sbatch` | real inference with the real prompt | a GPU | a job |
| `rc-kg/tests/` (pytest) | rc-kg units: sections, anchors, URLs, citations, topics... | nothing | seconds |
| `rc-kg/scripts/contract_test.sh` | a running rc-kg honours `API_CONTRACT.md` | rc-kg snapshot | ~1 min |
| `rc-kg/eval/*` | retrieval quality, reviewed-ticket regressions, graph integrity | a graph | seconds to minutes |

> **rc-agent:** adds `tests/agent/` (agent graph, guardrails, OpenTelemetry,
> prompt lint), `evals/run.py` (ground-truth evaluation), and
> `tests/run_all.sh`, the release gate that runs 13 of these in one command
> (12.9).

## 12.2 Conventions

All the Python test scripts share one shape, so their output reads the same:

```
  ok    cleaning keeps every comment and work note
  FAIL  the quoted chain is dropped

FAIL: 1 failure(s)
```

- One line per check: `  ok    <what>` or `  FAIL  <what>`.
- The last line is `PASS: 0 failure(s)` or `FAIL: <n> failure(s)`.
- Exit code 0 means pass, 1 means a check failed.
- They are plain scripts, not pytest: run them with the backend's Python,
  from `ticketmate/`.
- Every ticket in them is synthetic ("Ada Lovelace", `INC0000042`, job
  `4812345`). Keep it that way (chapter 11).

The shell suites (`run_contract.sh`, `run_slow_start.sh`, `ui/run.sh`) start
their own processes on free loopback ports, with a throwaway access token,
and stop everything and delete their work directory on exit (`trap cleanup
EXIT`). How well each is isolated from your real data differs:

| suite | run logs | spans, metrics | graph refresh loop |
|---|---|---|---|
| `run_contract.sh` | its work directory (`RUN_LOG_DIR`) | its work directory (`RC_TRACES_DIR`) | off (`RC_KG_REFRESH_LOOP=off`) |
| `rc-kg/scripts/contract_test.sh` | (no backend) | (no backend) | off |
| `tests/ui/run.sh` | its work directory | **not redirected**: `traces/$USER/` | **not turned off** |
| `run_slow_start.sh` | (no drafts) | (no drafts) | **not turned off** |

Until the last two are fixed, export `RC_TRACES_DIR=$(mktemp -d)` and
`RC_KG_REFRESH_LOOP=off` before running them.

## 12.3 Unit suites (no services)

### Ticket context

```bash
backend/.venv/bin/python tests/ticket_context/test_ticket_context.py
```

A synthetic ticket in the exact shape the extension scrapes from ServiceNow:
the activity stream newest first, notification emails listing 20 team
addresses, field changes, an email received, a signature and a quoted reply
chain. Checks that `ticket_context.prepare` keeps every comment and work
note, oldest first, labels authors as RC staff or researcher, drops
notifications, address lists, signatures and quoted chains, and that `fit`
keeps the first and newest entries within a token budget.

> **rc-agent:** also checks `ticket_context.expand`, the second search in
> documentation words: "pending" adds queue/priority/fairshare words, "OOM"
> adds memory words, a ticket with no symptom adds nothing, and the expansion
> never enters the query the model reads.

### Cluster lookups

```bash
backend/.venv/bin/python tests/lookups/test_lookups.py
```

The test writes fake `id`, `getent`, `sacct`, `scontrol`, `sinfo`, `squeue`,
`sshare`, `stat` and `ldconfig` into a temporary `bin/`, puts it first on `PATH`, and points `RC_LDCONFIG` and
`RC_GROUP_XTRA` at fakes too. Every fake appends its argv to a
`calls.jsonl`, and a `pwned` marker file would appear if anything injected
ever ran. So the test proves what actually executed: only allow-listed
programs, only with arguments that fully match their patterns, never through
a shell, whatever the ticket says. A fake that sleeps 30 s proves a timeout
is reported (`timed out`) within the limit, not raised.

### Playbooks

```bash
PLAYBOOK_DIR=<the private playbook directory> backend/.venv/bin/python tests/playbooks/test_playbooks.py
```

Tests RC's real playbooks, so it needs them. `cluster/env.sh` sets
`PLAYBOOK_DIR` on Explorer; without it the test prints `SKIP: RC's playbooks
are not available here (set PLAYBOOK_DIR)` and exits 0. Checks that every file
parses; each playbook fires on the requests it is for, worded as real tickets
word them, and not on look-alikes (a drained multigpu node is not a multigpu
access request); values are extracted; the runbook reaches the prompt only
when one matched.

### vLLM startup parser

```bash
cd backend && .venv/bin/python ../tests/startup/test_vllm_progress.py
```

Replays `tests/startup/vllm-0.29-startup.log`, a real vLLM 0.29 startup log,
one line at a time through `app.startup.vllm_progress`. Checks the stages
come in order (`starting`, `engine`, `weights`, `compile`, `profile`,
`graphs`, `server`), the percentage never goes backwards, weight shards are
counted, all four CUDA-graph passes are seen, and a healthy log is never
reported as failed.

## 12.4 Startup, end to end: `run_slow_start.sh`

```bash
tests/startup/run_slow_start.sh
RC_SLOW_START_TIMELINE=/tmp/timeline.json tests/startup/run_slow_start.sh    # also record the timeline
```

A session's startup as the extension sees it, without a GPU. It starts a
real rc-kg (writing its checklist to `RC_KG_PROGRESS_DIR`), the backend, and a
fake vLLM that writes the real 0.29 log to `VLLM_LOG_FILE` one line every
0.25 s (~40 s) and then serves the stub model. It polls `/api/health` every
second for up to 240 s, printing one line per poll, and checks: every
response carries `startup`; at least four model stages were reported while
loading; the model's bar never moved backwards and never reported failed;
the graph checklist was seen with a step running and became ready; the
session ended ready.

`RC_SLOW_START_TIMELINE` saves every `/api/health` snapshot; that file is the
input of `extension_startup_test.py` (12.5).

## 12.5 Browser tests (Playwright)

### Setting up Playwright

Playwright is kept out of the backend venv. Once, on `/scratch`:

```bash
python3 -m venv /scratch/$USER/pw_venv && /scratch/$USER/pw_venv/bin/pip install playwright
PLAYWRIGHT_BROWSERS_PATH=/scratch/$USER/pw_browsers /scratch/$USER/pw_venv/bin/playwright install chromium
```

| variable | default | used by |
|---|---|---|
| `PW_PYTHON` | `/scratch/$USER/pw_venv/bin/python` | `tests/ui/run.sh` (exit 2 if it is missing) |
| `PLAYWRIGHT_BROWSERS_PATH` | `/scratch/$USER/pw_browsers` (set by `run.sh`) | Playwright, to find Chromium. Export it yourself when running a test file directly |
| `SHOTS` | `/tmp` | where screenshots go |

### `tests/ui/run.sh`

```bash
tests/ui/run.sh
```

Starts rc-kg, the stub model and a backend (access token `uitest`), waits
until `/api/health` says the knowledge graph is `ok`, then runs:

| file | what it checks |
|---|---|
| `web_feedback_test.py` | the backend's web page: sign in, draft, the cluster lookups card (when `$USER` looks like an Explorer username), the automatic checks summary (a failed assertion is named and styled as a warning), the 8-character request id, the feedback form, and what the backend stored beside the run |
| `extension_result_test.py` | the extension panel's result layout with a mocked service worker: half the page wide; where the draft went, confidence, what to check and the draft shown at once; everything else in collapsible sections that are remembered once opened; cited pages as links |

### `extension_form_test.py`

```bash
PLAYWRIGHT_BROWSERS_PATH=/scratch/$USER/pw_browsers \
  /scratch/$USER/pw_venv/bin/python tests/ui/extension_form_test.py
```

Not part of `run.sh`; run it after any change to how `content.js` reads a
page. Four mock ticket pages, served by Playwright's router on
`servicenow.test`; the service worker is mocked and the text the panel would
send is captured:

| page | what it proves |
|---|---|
| `classic` | a Classic-style variable editor: read-only inputs, a drop-down's selected text, a checkbox as yes/no, a reference field's display value (never its hidden sys_id) |
| `portal` | a Service Portal-style form: the label in the form group, `*` dropped, a label by `aria-labelledby`, a radio group's chosen answer |
| `textonly` | a read-only "Variables" section rendered as text is kept as text |
| `incident` | an incident with no form is sent unchanged, and the panel does not mention a form |

The answers appear under `Request form:` in the sent text. No page errors are
allowed.

### `extension_startup_test.py`

```bash
RC_SLOW_START_TIMELINE=/tmp/timeline.json tests/startup/run_slow_start.sh
PLAYWRIGHT_BROWSERS_PATH=/scratch/$USER/pw_browsers \
  /scratch/$USER/pw_venv/bin/python tests/ui/extension_startup_test.py /tmp/timeline.json [shots_dir]
```

Feeds each recorded `/api/health` snapshot through the real `ping()` and
`request()` lifted from `extension/background.js`, into the shipped
`content.js` and `content.css`, and checks the panel's startup progress:
the model stage shown, the graph checklist's 8 steps, "connected" with a note
while the graph is still loading, the progress block gone once all is ready.
It adds synthetic states the recording did not pass through (the graph still
warming up, a model that failed), and checks how OOD's error pages are told
apart (the backend not up vs the SSO login page).

### `extension/check.py`

```bash
backend/.venv/bin/python extension/check.py
```

Static checks, each for something that once shipped broken: duplicate
`const`/`let` (a duplicate made the content script throw at load), orphaned
`var(--rc-*)` CSS tokens, selectors the markup no longer emits. Needs
`esprima`, which is in the backend venv. `extension/package.sh` runs it and
refuses to package on failure.

## 12.6 The contract test: `tests/rc_kg/run_contract.sh`

```bash
tests/rc_kg/run_contract.sh
RC_CONTRACT_KEEP_RUNS=/tmp/runs tests/rc_kg/run_contract.sh     # keep the run logs it wrote
```

The consumer side of rc-kg's API contract, and the widest test of the
backend. It starts three stub models (`tests/rc_kg/stub_vllm.py`: normal;
`STUB_REJECT_SCHEMA=1`, which rejects `json_schema`; `STUB_NO_TOOLS=1`, which
refuses tool calls), two real rc-kg instances (full and website-only graph),
a fake rc-kg claiming API v2.0 (`fake_kg_v2.py`), and one backend per
scenario, each with access token `contract` and `USE_PROXY_ENV=false`.

| scenario | backend talks to | checks |
|---|---|---|
| **A** | real rc-kg, normal stub | health `ok` and startup ready; draft grounded; references `{n,title,url}` above the sign-off; no raw `[S#]`; the invented URL removed and reported; correct citations not flagged |
| A, trace | | one run log, `trace_version` 3, same `request_id` as the API; token totals add up and match the API's `usage`; prompt token breakdown; final attempt's raw output, latency, outcome; retrieval sources with URLs; rc-kg's trace carries the same `request_id`; graph version; citations; model output before and after citation; per-stage timings; TicketMate version; version-1 fields kept |
| A, run tree | | one root chain run `draft`; every parent exists; every run finished; retrieve, generate, llm, parse, cite, tokenize steps; rc-kg's stages nested under retrieve; the retriever run's documents; the llm run's messages and params; project/thread/session/prompt version; automatic feedback |
| A, feedback | | `POST /api/feedback` stored beside the run; unknown draft is a 404; comment-only feedback accepted; a corrected reply gives `edit_similarity` just under 1 |
| A, evaluators | | 9 or more assertions returned; sign-off and no-placeholder pass; `assert_urls_from_sources` fails on the invented URL; conciseness, embedding relevance, hallucinated URLs; the background LLM judge records hallucination, correctness, conciseness, answer relevance with direction and tokens |
| A, citations | | on a GPU-queue ticket, a wrong citation is flagged and a correct one is not |
| **B** | fake rc-kg v2.0 | health `incompatible` naming the version; draft ungrounded with a caveat giving the reason; no raw markers |
| **C** | nothing (a free port) | health `unreachable`; draft still produced, with a caveat |
| **D** | website-only rc-kg | health names the website graph; every reference on `rc.northeastern.edu` |
| **E** | real rc-kg, stub rejecting `json_schema` | both attempts in the trace in order (`json_schema`, `json_object`); the rejected one has HTTP 400 and no usage; tokens counted only from the call that returned usage; the run tree shows an error run then a success run |

> **rc-agent:** two more scenarios.
> **F** (real rc-kg, normal stub): the orchestrator routed the ticket to
> `incident` and says why; the agent planned then finished; its tool calls
> were `search_knowledge_graph` then `lookup_job`; the hostile job id was
> refused by the schema and `/tmp/stub_pwned` never appeared; the critic and
> the reviewer both reviewed; the path through the graph and the versions of
> every prompt, skill and tool are recorded; every node, tool and review is a
> run in the trace; the panel gets the tool calls.
> **G** (stub without tool calls): the agent stopped with `tools unsupported`
> and the pipeline's draft is still produced and grounded.

A pass ends with `PASS: 0 failure(s)`. Most of the time is rc-kg starting.

## 12.7 The inference test (GPU)

```bash
cd ticketmate/tests
sbatch --gres=gpu:t4:1 --export=ALL,MODEL=small run_inference_test.sbatch
tail -F logs/inftest-<jobid>.out
```

The only test that needs a GPU. It starts vLLM on loopback, then runs
`inference_test.py`: a model is served; a trivial completion works; whether
the model returns a separate reasoning field; the real prompt with guided
decoding, then without; not truncated; JSON valid against the product's own
Pydantic model; confidence in range; draft non-trivial. It prints the whole
draft and ends with `RESULT: PASS ...` or `RESULT: FAIL ...`. Name the GPU
type: V100s cannot run it. `TICKET=<file>` and `INSTRUCTIONS="..."` try a
ticket of your own; `python inference_test.py --base-url
http://127.0.0.1:8000/v1` runs it against a server already up.
`tests/README.md` has the details.

## 12.8 rc-kg's tests and evals

### Unit tests

```bash
cd rc-kg && .venv/bin/python -m pytest -q tests
```

No Neo4j, no network. One file per concern:

| file | covers |
|---|---|
| `test_sections.py`, `test_anchors.py` | section splitting; anchor resolution (real `#id` or text fragment) |
| `test_urls.py` | URL normalisation, the join key between a link and a page |
| `test_duplicates.py` | repeated sections: one retrievable copy, the rest point at it |
| `test_not_retrievable.py` | people, leadership news and feed pages: kept in the graph, never retrieved |
| `test_rtd.py` | rc-docs (Sphinx) ingestion: URL mapping, cleanup, anchors |
| `test_topics.py` | topic matching, the only way a `MENTIONS` edge or a query topic is made |
| `test_fulltext_query.py` | the Lucene query built from ticket text |
| `test_citations.py` | links come from retrieval, never from the model |
| `test_support.py` | claim extraction for the citation-support check |
| `test_metrics.py` | metric formulas (the RAGAS expectations computed with ragas 0.4.3) |
| `test_ragas_samples.py` | run logs become RAGAS samples (with `stub_judge.py`) |

### API contract

```bash
rc-kg/scripts/contract_test.sh                    # the full graph
rc-kg/scripts/contract_test.sh --variant website  # the website-only graph
```

Starts a fresh rc-kg from the snapshot (`RC_KG_REFRESH_LOOP=off`), waits for
`/health` to be `ok`, and runs `eval/contract_check.py` against
`API_CONTRACT.md`. Exit 0 means the v1 contract holds. This is the producer
side; `run_contract.sh` (12.6) is the consumer side.

### Evals (need a graph)

All run inside a private copy of the published graph with
`scripts/with_kg.sh`, from `rc-kg/`:

| command | what it measures |
|---|---|
| `scripts/with_kg.sh -m eval.run` | Hit@k and MRR on the 45 questions in `eval/questions.yaml`; the build gate (`--min-hit`) |
| `scripts/with_kg.sh -m eval.ticket_cases` | regressions from reviewed tickets (`eval/ticket_cases.yaml`); `--all` prints every case with its top k |
| `scripts/with_kg.sh -m eval.retrieval_eval` | RAGAS context metrics and ranking metrics, with confidence intervals |
| `scripts/with_kg.sh -m eval.graph_eval` | graph integrity (a hard gate at build), usefulness, topics, topology; `--live` also checks links on the live sites |
| `scripts/with_kg.sh -m eval.citation_check` | the citation-support check: false flags and catches |
| `scripts/with_kg.sh -m eval.variants collect <out.json>` | full vs website-only graph on the same questions and tickets |
| `scripts/with_kg.sh -m eval.optimize [--quick]` | a sweep of retriever settings |
| `eval/ragas_llm.py`, `eval/citation_review.py`, `eval/draft_batch.py` | LLM-judged RAGAS; citation accuracy on real drafts; sending the questions through a running TicketMate |

`eval.ticket_cases` prints only failures, then `<n>/<m> cases pass`, and exits
1 if any failed.

## 12.9 rc-agent: agent tests, evals and the release gate

> **rc-agent:** everything in this section exists only in rc-agent.

### Agent tests

```bash
backend/.venv/bin/python tests/agent/test_agent.py        # tools, ReAct, orchestrator, critic, reviewer, whole graph
backend/.venv/bin/python tests/agent/test_guardrails.py   # input and output guardrails
backend/.venv/bin/python tests/agent/test_otel.py         # run tree -> OpenTelemetry spans
backend/.venv/bin/python tests/agent/test_prompts.py      # prompt lint
```

`test_agent.py` uses a scripted model that answers each agent by name and the
same fake read-only binaries as the lookups test. `test_otel.py` exports a run
tree to an in-memory exporter and checks spans, parents, GenAI attributes, and
that no ticket text is exported unless `OTEL_EXPORT_CONTENT` is set.
`test_prompts.py` checks placeholders, length budgets, that the safety rules
are stated, and that `RC_AGENT_PROMPTS` overrides only same-named prompts.
`tests/agent/replay_eval.py` re-drafts past tickets on a GPU job; chapter 17.

### Evals

```bash
backend/.venv/bin/python evals/run.py                      # rag + routing + guardrails (no GPU)
backend/.venv/bin/python evals/run.py --suite e2e          # full drafts; needs VLLM_BASE_URL
backend/.venv/bin/python evals/run.py --suite rag --gate   # exit 1 below a floor
```

Scores against `evals/groundtruth.yaml`; reports go to
`traces/$USER/evals/<time>-<suite>/`. Chapter 17 has the suites and floors.

### The release gate: `tests/run_all.sh`

```bash
tests/run_all.sh            # all 13 stages (~3 min, most of it rc-kg starting)
tests/run_all.sh --quick    # the first 10, unit tests only (seconds)
```

| # | stage | runs |
|---|---|---|
| 1 | ticket context | `tests/ticket_context/test_ticket_context.py` |
| 2 | cluster lookups | `tests/lookups/test_lookups.py` |
| 3 | skills (playbooks) | `tests/playbooks/test_playbooks.py` |
| 4 | agent graph | `tests/agent/test_agent.py` |
| 5 | OpenTelemetry export | `tests/agent/test_otel.py` |
| 6 | guardrails | `tests/agent/test_guardrails.py` |
| 7 | prompt lint | `tests/agent/test_prompts.py` |
| 8 | eval: routing+guardrails | `evals/run.py --suite routing --suite guardrails --gate` |
| 9 | rc-kg unit tests | `rc-kg/.venv/bin/python -m pytest -q rc-kg/tests` |
| 10 | rc-kg same as TicketMate's | `diff -rq` of rc-kg against TicketMate's, excluding caches, venvs, `vendor`, `data`, `.env`: both apps publish to one shared graph store, so their rc-kg code must be identical |
| 11 | rc-kg ticket regressions | `scripts/with_kg.sh -m eval.ticket_cases` |
| 12 | eval: RAG on ground truth | `evals/run.py --suite rag --gate` |
| 13 | end-to-end contract (A-G) | `tests/rc_kg/run_contract.sh` |

Each stage prints one line; a failing stage also prints up to 15 lines of its
output that match `FAIL`, `Error` or `error`. The script exits 0 only if every
stage passed:

```
ticket context                     PASS
cluster lookups                    PASS
...
end-to-end contract (A-G)          PASS

ALL PASS
```

Run it before `ood-apps/publish.sh` and after any change to prompts, skills
or tools.

TicketMate has no `run_all.sh`; run its suites one by one (12.10).

## 12.10 When to run what

| you changed | run |
|---|---|
| `ticket_context.py` | ticket context; then the contract test |
| `lookups.py` | lookups; then the contract test |
| a playbook, or `playbooks.py` | playbooks (with `PLAYBOOK_DIR`) |
| `prompts.py`, `llm_client.py`, `models.py` | the contract test; the inference test on a GPU before shipping |
| `kg_client.py`, `evaluators.py`, `runlog.py`, `tracing.py`, `main.py` | the contract test |
| `startup.py` | `test_vllm_progress.py`, `run_slow_start.sh` |
| `content.js`, `content.css`, `background.js` | `extension/check.py`, `tests/ui/run.sh`, `extension_form_test.py`, `extension_startup_test.py` |
| `backend/web/` | `tests/ui/run.sh` |
| rc-kg retrieval or ingest | rc-kg pytest, `eval.run`, `eval.ticket_cases`, `contract_test.sh`, then `run_contract.sh` |
| rc-kg API | `contract_test.sh` and `run_contract.sh` |
| `topics.yaml` | `test_topics.py`, `eval.run`, `eval.graph_eval` |
| anything, before a release | everything above that needs no GPU |

> **rc-agent:** for anything, `tests/run_all.sh --quick` while working and
> `tests/run_all.sh` before committing or publishing. A change to an agent's
> prompt, a skill or a tool that affects what the model does also needs
> `evals/run.py --suite e2e` (or `evals/prompt_ab.py`) on a GPU job before it
> is adopted.
