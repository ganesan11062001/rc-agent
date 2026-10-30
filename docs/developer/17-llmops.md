---
name: "17. rc-agent: LLMOps"
route: /llmops
menu: rc-agent
---

# 17. rc-agent: LLMOps

How rc-agent's drafts are recorded, exported, measured, evaluated and tuned,
and the one command that gates a release. The reference summary is
`ticketmate/LLMOPS.md`; this chapter explains the code behind it.

Several of these parts (`app/ops_store.py`, `app/otel.py`, `ops/ops_report.py`,
the run log) are the same files in TicketMate. What is rc-agent's own: the
agent provenance and manifest in each trace, the guardrails log, the prompt
registry, the ground truth and `evals/`, the agent tests and
`tests/run_all.sh`. Chapter 18 has the full list.

## 17.1 The trace store

`app/ops_store.py` keeps everything under one directory per user, beside
the code:

```
traces/$USER/                       0700; every file 0600; traces/ is git-ignored
  runs/<ticket>/<time>-<id>.json      the full run log of each draft  (app/runlog.py)
  runs/<ticket>/<time>-<id>.feedback.jsonl   people's, the judge's, assertions, metrics
  spans/YYYY-MM-DD.jsonl              OpenTelemetry spans of every draft  (app/otel.py)
  metrics/YYYY-MM-DD.jsonl            one row per draft, no ticket text
  guardrails/YYYY-MM-DD.jsonl         every guardrail finding with its request id
  evals/<YYYYmmdd-HHMMSS>-<suite>/    evaluation, optimisation and ops reports
```

| function | does |
|---|---|
| `root()` | `$RC_TRACES_DIR`, else `<repo>/traces/$USER` (`REPO_ROOT` is three levels above `app/`) |
| `path(kind)` | creates `root()/<kind>` and chmods it and `root()` to 0700; `None` if it cannot |
| `append(kind, row)` | appends one JSON line to `<kind>/<date>.jsonl`, opened with mode 0600; never raises |
| `new_eval_dir(suite)` | `evals/<YYYYmmdd-HHMMSS>-<suite>/`, 0700; the suite name keeps only letters, digits, `-` and `_` (40 chars) |

Why on `/projects`: "so the team's traces are where the code is and
survive /scratch purges". Why the permissions: "Tickets carry researchers'
data, so the per-user directory is 0700 and every file 0600". Writing is
best-effort: "a full disk never breaks a draft".

| variable | default | effect |
|---|---|---|
| `RC_TRACES_DIR` | `<repo>/traces/$USER` | moves the whole store |
| `RUN_LOG_DIR` | `<RC_TRACES_DIR>/runs` | moves the run logs alone; empty turns them off |
| `RC_TRACES_SPANS` | `on` | `off`, `0`, `false`, `no`: no local span files |

The OOD app's `script.sh.erb` leaves `RUN_LOG_DIR` unset, so a session
records under `<repo>/traces/$USER/runs` like everything else. The evals set
`RUN_LOG_DIR=""` (no run logs) and the contract test sets `RC_TRACES_DIR` to
a temporary directory.

## 17.2 The run log

`runlog.record` writes one JSON file per draft, grouped by ticket. Besides
what TicketMate records (input, exact prompts, raw output, draft, error,
model, latency, strategy), the `trace` object holds:

| key | contents |
|---|---|
| `request_id`, `started_at` | |
| `versions` | the code's commit (`-dirty` with local changes), the OOD release, rc-kg's version and snapshot |
| `ticket_context` | cleaning stats, window, ticket budget, `sources_packed`, `route`, `revision`, `agent` summary |
| `retrieval` | rc-kg's trace, with `expansion` and `merged_searches` when there was a second search |
| `cluster_lookups`, `playbooks` | what the final draft was written from (the rules' plus the agents') |
| `agent` | rc-agent: `graph.provenance` -- route and reason, plan, stop reason, steps, tool calls, reflections, reviews, revisions, path, evidence summary, agent token usage, latency, manifest |
| `guardrails` | rc-agent: every input and output finding |
| `llm` | the writer's parameters, final strategy and every attempt (both passes on a revision) |
| `tokens` | totals over the writer's attempts, `model_calls`, the final call, the prompt breakdown |
| `citations`, `output` | the citation trace; the draft before and after citations |
| `timings_ms` | `retrieve`, `lookups`, `llm` (the whole graph in agent mode), `citations`, `total` |
| `run_tree` | the LangSmith-style tree (chapter 15.13), with feedback |

The root run's metadata carries `project` (`rc-agent/<graph variant>` or
`rc-agent/no-kg`), `thread_id` (the ticket), `session_id` (the Slurm job),
node, GPU, model, the writer's `prompt_version`, and a `config` snapshot
with no secrets: `agent_mode`, `agent_max_steps`, `agent_max_tool_calls`,
`agent_max_revisions`, `agent_reviewer`, `playbooks`, `cluster_lookups`,
`ticket_context_cleaning`, `temperature`, `max_tokens`, `ticket_max_tokens`,
`rc_kg_k`.

Root tags that are rc-agent's: `route:<route>`, `agent:<stopped>`,
`agent_calls:<n>`, `revised`, `guardrail:<rule>`.

`trace.tokens` counts the writer's calls only (TicketMate's `llm`
attempts). The agents' calls -- orchestrator, plan, reasoning, reviewer --
are summed in `trace.agent.usage`, and each is an `llm` run in the tree.

`cluster/traces.py` (`list`, `show`, `runs`, `stats`, `feedback`, `export`)
reads run logs; its default directory is the backend's: `$RUN_LOG_DIR`, else
`$RC_TRACES_DIR/runs`, else `<repo>/traces/$USER/runs`.

## 17.3 Versions: the prompt registry and the manifest

Every draft records the version of everything it depended on, so "two
drafts can be compared knowing exactly what differed" (`LLMOPS.md`).

| what | version | where |
|---|---|---|
| each agent prompt (`app/agents/prompts/<name>.md`) | the first 8 hex digits of the SHA-256 of the file's text (stripped, plus a newline) | `prompt_registry.load_all`; the manifest's `prompts` |
| each skill | 8 hex digits of the SHA-256 of its `SkillInfo` as sorted JSON | the manifest's `skills` |
| each tool | 8 hex digits of the SHA-256 of its function-calling schema as sorted JSON | the manifest's `tools` |
| the writer's prompt | 12 hex digits over every upper-case string constant in `app/prompts.py` | `tracing.prompt_version`; root metadata `prompt_version` |
| the code | git commit | `versions.ticket_mate` |

`graph.manifest()` builds the first three; it is in the `graph` run's
metadata and in `trace.agent.manifest`. The metrics row and the eval
reports keep the counts (`{"prompts": 9, "skills": <n>, "tools": 12}`); the
eval `report.json` also keeps the full manifest.

`prompt_registry.load_all(directory, override)` reads every `*.md` in
`app/agents/prompts/`, then every `*.md` in the override directory
(`$RC_AGENT_PROMPTS`), which replaces same-named prompts and nothing else.
It is `lru_cache`d: a process reads the prompts once. `get(name)` raises on
an unknown name; `Prompt.render(**values)` raises on a missing placeholder.

`tests/agent/test_prompts.py` (prompt lint) checks:

- the prompt files are exactly the nine known ones;
- each template fills exactly its placeholders (`react_user`: ticket,
  instructions, gathered; `react_act`: plan; `react_reflect`: gaps;
  `reviewer_user`: ticket, lookups, sources, skills, summary, steps, reply;
  `react_plan`: none);
- each system prompt is under 3,200 characters ("a 7B model with a 16-32k
  window reads every token of them on every turn") and has an 8-digit
  version;
- the incident and request prompts say they are read-only and forbid
  guessing names;
- the orchestrator states its JSON and has examples; the reviewer states its
  JSON and that it does not rewrite;
- the writer's system prompt states the no-false-claims and no-redirect
  rules, and its built prompt has no unfilled placeholder;
- a variant directory overrides only what it has.

## 17.4 OpenTelemetry

`app/otel.py` exports each draft's run tree as OpenTelemetry spans after
the draft is recorded (`record()` in `main.draft` calls
`otel.export(tr.to_dict())`). Export never raises.

| destination | when | processor |
|---|---|---|
| `traces/$USER/spans/<date>.jsonl` | unless `RC_TRACES_SPANS` is off | `SimpleSpanProcessor` with `FileSpanExporter` (each span's `to_json()` through `ops_store.append`) |
| an OTLP/HTTP collector | `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` is set | `BatchSpanProcessor` with `OTLPSpanExporter` |

The module docstring names Jaeger, Grafana Tempo, Arize Phoenix, Langfuse
and Datadog as collectors. The resource is `service.name` =
`$OTEL_SERVICE_NAME` (default `rc-agent`), `service.namespace` =
`research-computing`; the tracer is `rc-agent.drafts`.

One trace per draft; one span per run, created in run order (parents before
children) with the run's real start time and parent, and ended in reverse
order with its real end time. A run in error gets status `ERROR` with its
message (300 characters). Each numeric feedback score is an event
`feedback` (`key`, `score`, `source`) on the run it rates, else the root.

| attribute | on | value |
|---|---|---|
| `gen_ai.operation.name` | `llm` runs | `chat` |
| `gen_ai.system` | `llm` runs | `ls_provider` (`vllm`) |
| `gen_ai.request.model` | `llm` runs | `ls_model_name` |
| `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` | `llm` runs | `prompt_tokens`, `completion_tokens` |
| `gen_ai.operation.name` | `tool.<name>` runs | `execute_tool` |
| `gen_ai.tool.name` | `tool.<name>` runs | `<name>` |
| `rc.run_id`, `rc.run_type`, `rc.status` | every span | |
| `rc.request_id`, `rc.thread_id`, `rc.project` | every span | from the root's metadata |
| `rc.tags` | runs with tags | up to 50 |
| `rc.meta.<key>` | every span | the run's scalar metadata (strings to 500 chars; lists of scalars to 50 items) |
| `rc.latency_ms` | every span | |
| `rc.inputs`, `rc.outputs` | only with `OTEL_EXPORT_CONTENT=true` (`1`, `yes`) | JSON, to 4,000 characters |

Content is off by default: "tickets carry researchers' data, and a
collector is usually shared."

`tests/agent/test_otel.py` builds a run tree and exports it to an in-memory
exporter: one span per run, the same parents, real timestamps, GenAI
attributes, errors as status, feedback as events, no ticket text unless
asked.

## 17.5 Metrics

`main.draft` appends one row per draft to `traces/$USER/metrics/` -- no
ticket text:

| field | |
|---|---|
| `ts`, `request_id`, `ticket`, `model` | `ticket` is the run log's folder name |
| `mode` | `agent` or `pipeline` |
| `route`, `route_reason` (80 chars), `agent_stopped` | |
| `tool_calls`, `tool_refusals` | tool names; calls with `ok` false |
| `reflections`, `revisions` | counts |
| `reviews` | `{reviewer: ok}` over the last three reviews |
| `reviewer_supported_share` | the last reviewer score |
| `guardrails` | rule names |
| `kg_status`, `sources`, `low_confidence`, `references`, `urls_removed` | retrieval and citations |
| `playbooks`, `lookups` | skill names; lookup count |
| `assertions_failed`, `confidence` | |
| `latency_ms`, `timings_ms`, `tokens` | |
| `prompt_version`, `manifest` | the writer's version; counts of prompts, skills, tools |

`ops/ops_report.py` summarises them:

```bash
backend/.venv/bin/python ops/ops_report.py                 # the last 7 days, your traces
backend/.venv/bin/python ops/ops_report.py --days 30 --user all
```

It prints and writes `traces/$USER/evals/<time>-ops/ops.md`: drafts (agent
vs pipeline) and tickets, the route mix, latency p50/p95, mean time by
stage, tokens p50/p95, stop reasons, tool calls per draft and refusals,
tools used, reviews passed in the last round, drafts revised, reviewer
groundedness (median supported share), guardrails fired, knowledge-graph
health, skills used, failed assertions, models and prompt versions.

## 17.6 Feedback

As in TicketMate, each draft's trace collects feedback in its
`.feedback.jsonl`: people's (`POST /api/feedback`: a key, score, comment and
the reply as sent, whose edit similarity is computed), the background LLM
judge's, and the assertions and metrics computed at draft time. rc-agent
adds `guardrail_findings`. The judge is the same model grading itself; the
critic's docstring records that it "gave correctness 1.0 to drafts the team
marked as failures" -- which is why the critic is rules.

## 17.7 The ground truth

`evals/groundtruth.yaml` is the labelled set: 22 cases, "built 2026-10-28
from tickets the RC team reviewed ... reworded so no real person, username
or NUID remains, plus a few synthetic cases for procedures the run logs do
not have yet". Six are marked synthetic. Routes: 9 `incident`, 12
`request`, 1 `followup`.

A case:

```yaml
- id: gt-example
  source: synthetic
  subject: Job killed
  description: |
    My job 4812345 was killed and sacct says OUT_OF_MEMORY. I asked for --mem=4G.
  thread:                     # optional, oldest first
    - {who: "Lovelace, Ada", staff: false, kind: comment, body: "..."}
  instructions: ""            # optional: what the engineer typed
  route: incident
  must_lookup: [job:4812345]
  expected_sources: [https://rc.northeastern.edu/...]
  reply_must: ["(?i)--mem|memory"]
```

| label | scored by | meaning |
|---|---|---|
| `route` | routing, e2e | `incident`, `request` or `followup` |
| `skills`, `skills_not` | routing, e2e | skills that must (not) be used |
| `must_lookup` | e2e | `<kind>:<target>` lookups that must be made |
| `expected_sources` | rag, e2e | pages any one of which must be retrieved |
| `must_not_sources` | rag, e2e | pages that must not be retrieved |
| `low_confidence` | rag | the flag must (not) be up |
| `must_ask` | e2e | words the reply must ask about (with a `?`) |
| `reply_must`, `reply_must_not` | e2e | regexes on the reply |
| `reference_reply` | e2e | for similarity |
| `guardrails` | guardrails | input rules that must fire |

`defaults` apply to every case: `never_sources` (pages that are never an
answer), a `reply_must` for the sign-off, and `reply_must_not` patterns for
placeholders and claimed actions. A case is scored only on the labels it
has.

`run.compose(case)` turns a case into the page the extension would send:
`Ticket: GT<digits>`, short description, description, then a `Work notes /
activity` stream, newest first, opening with a ServiceNow notification
email (to a made-up team list) for the cleaner to remove, and each entry
labelled *Work notes* or *Additional comments*. The cases are then cleaned
by the real `ticket_context.prepare`, so the eval goes through the same
cleaning as a draft.

## 17.8 `evals/run.py`

```bash
cd ticketmate
backend/.venv/bin/python evals/run.py                         # rag + routing + guardrails (no GPU)
backend/.venv/bin/python evals/run.py --suite rag             # one suite (repeatable)
backend/.venv/bin/python evals/run.py --suite e2e             # full drafts (needs vLLM: VLLM_BASE_URL)
backend/.venv/bin/python evals/run.py --compare <report.json> # with the change from a baseline
backend/.venv/bin/python evals/run.py --gate                  # exit 1 if a gated metric misses its floor
backend/.venv/bin/python evals/run.py --groundtruth <file>    # another labelled set
```

It needs RC's playbooks: without `PLAYBOOK_DIR` (or
`/projects/rc/projects/rc-playbooks`) it prints `SKIP` and exits 0. For
`rag` and `e2e` it uses `RC_KG_URL`, or starts a private rc-kg with
`rc-kg/scripts/serve_kg.sh` on a free port (`RC_KG_REFRESH_LOOP=off`: "an
eval never rebuilds or switches the graph") and polls its `/health` up to
150 times, 2 s apart.

| suite | needs | runs | metrics |
|---|---|---|---|
| `rag` | rc-kg | `kg_client.retrieve_multi` on each case with RAG labels, through `prepare` | `cases`, `hit_at_k`, `mrr`, `recall_at_k`, `forbidden_rate`, `low_conf_precision`, `low_conf_recall`, `context_chars_median` |
| `routing` | -- | `skills.trigger` on each case; `graph._route` with the model call replaced by one that fails, so only the rules decide | `skill_accuracy`, `rule_route_accuracy`, `rule_routed_share` |
| `guardrails` | -- | `guardrails.check_input` on each case | `detected`, `false_positives` |
| `e2e` | vLLM, rc-kg | `main.draft` on each case: the agent graph, or the pipeline with `AGENT_MODE=false` | `cases`, `errors`, `route_accuracy`, `skill_recall`, `lookup_recall`, `cited_hit`, `reply_checks_pass`, `forbidden_claims`, `reviews_passed`, `revised_share`, `latency_s_median`, `tokens_median`, `similarity_median` |

Scoring details:

- Pages are compared without the `#fragment` and trailing `/`.
- `rag` scores every source the writer gets (up to 8 after the second
  search); `K = 6` is the label.
- `e2e` scores retrieval on the references actually cited (`cited_hit`).
- `reply_checks_pass` is 1 for a case when no `reply_must` failed, no
  `reply_must_not` matched and every `must_ask` is asked.
- `forbidden_claims` counts `reply_must_not` matches.
- `similarity` is `difflib.SequenceMatcher` ratio against `reference_reply`.

### Gates

`--gate` fails if a metric present in the report misses its floor
(`FLOORS`); metrics a run did not compute are skipped.

| metric | gate |
|---|---|
| `rag.hit_at_k` | >= 0.85 |
| `rag.forbidden_rate` | <= 0.0 |
| `rag.low_conf_recall` | >= 0.8 |
| `routing.skill_accuracy` | >= 0.9 |
| `routing.rule_route_accuracy` | >= 1.0 |
| `guardrails.detected` | >= 1.0 |
| `e2e.route_accuracy` | >= 0.85 |
| `e2e.reply_checks_pass` | >= 0.85 |
| `e2e.forbidden_claims` | <= 0 |

### Reports

`traces/$USER/evals/<time>-<suites>/`: `report.json` (mode, manifest,
version counts, every suite's metrics and rows), `report.md` (the metrics
table, with baseline and change under `--compare`, the gate verdict, and
each failing case), `rows.jsonl`. The latest reports (2026-10-29):

| metric | value |
|---|---|
| `rag.cases` | 13 |
| `rag.hit_at_k` | 1.0 |
| `rag.mrr` | 0.714 |
| `rag.recall_at_k` | 0.9 |
| `rag.forbidden_rate` | 0.0 |
| `rag.low_conf_precision`, `rag.low_conf_recall` | 1.0, 1.0 |
| `routing.skill_accuracy` | 1.0 |
| `routing.rule_route_accuracy` | 1.0 |
| `routing.rule_routed_share` | 0.636 |
| `guardrails.detected` | 1.0 (0 false positives) |

No `e2e` report has been recorded yet.

## 17.9 Other evaluations

| command | what | needs |
|---|---|---|
| `cd rc-kg && scripts/with_kg.sh -m eval.run` | 45 researcher-style questions (`eval/questions.yaml`); the graph build gate | the graph |
| `cd rc-kg && scripts/with_kg.sh -m eval.ticket_cases` | 12 reviewed-ticket regressions (`eval/ticket_cases.yaml`) | the graph |
| `cd rc-kg && scripts/with_kg.sh -m eval.retrieval_eval` | RAGAS context precision/recall and ranking metrics, with 95% bootstrap intervals | the graph |
| `backend/.venv/bin/python tests/agent/replay_eval.py --runs <dir> --limit 20 --out replay.jsonl` | re-draft past tickets from their run logs | vLLM, rc-kg |

`replay_eval.py` takes each ticket's latest run log (default directory:
TicketMate's `traces/$USER/runs`), drafts it again through `main.draft`, and
writes one row per ticket: route, stop reason, tool calls, revisions,
reviews, knowledge-graph status, references, confidence, skills, latency,
tokens, and similarity to the reply actually sent (the `correction` on the
engineer's feedback). Run it with `AGENT_MODE=false` for the pipeline
baseline. The report holds ticket numbers, routes, tool names and scores,
not ticket text; it sets `RUN_LOG_DIR=""` so replays are not mixed into the
real run logs.

```bash
VLLM_BASE_URL=http://127.0.0.1:8000/v1 RC_KG_URL=http://127.0.0.1:<port> \
  backend/.venv/bin/python tests/agent/replay_eval.py \
    --runs /projects/rc/projects/ticketmate/traces/$USER/runs --limit 20 --out replay.jsonl
```

## 17.10 Optimisation

### RAG: `rc-kg/eval/optimize.py`

Sweeps the retriever's module constants in `retrieval/graphrag.py`, patched
per run, "nothing is changed on disk":

| constant | default grid | `--quick` |
|---|---|---|
| `_RRF_K` | 30, 60, 90 | 60 |
| `_FULLTEXT_WEIGHT` | 0.7, 1.0, 1.4 | 0.7, 1.0, 1.4 |
| `_TOPIC_WEIGHT` | 0.0, 0.3, 0.6 | 0.0, 0.3 |
| `_MAX_PER_PAGE` | 2, 3 | 2, 3 |
| `_STALE_FACTOR` | 0.6 | 0.6 |

Each setting is measured on three sets at once, "so a setting cannot win on
one by losing another": the 45 questions (Hit@5, MRR), the 12 regressions
(cases passing) and the ground truth through TicketMate's query building
(Hit@6, MRR, forbidden pages). The score is questions MRR + ground-truth
MRR; a setting that fails a regression or retrieves a forbidden page is
disqualified. It prints the current setting and the best five.

```bash
cd rc-kg
scripts/with_kg.sh -m eval.optimize --quick --json ../traces/$USER/evals/rag-optimize-<date>.json
```

Adopt a winner by hand, copying it into `graphrag.py` with the numbers in a
comment. The run of 2026-10-27 (`rag-optimize-20261027-2047.json`) scored
the current setting (`_RRF_K` 60, `_FULLTEXT_WEIGHT` 1.0, `_TOPIC_WEIGHT`
0.3, `_MAX_PER_PAGE` 3) 1.495 and the best (30, 0.7, 0.3, 2) 1.51, with the
same Hit@5, regressions and ground-truth MRR; the difference is 0.015 in
questions MRR.

The ground-truth part imports `ticketmate/evals/run.py`, so it runs only in
rc-agent's checkout. Its ground-truth queries use one search on the query,
not `retrieve_multi`.

### Prompts: `evals/prompt_ab.py`

```bash
mkdir -p variants/orchestrator-v2 && $EDITOR variants/orchestrator-v2/orchestrator.md
# on a GPU job with vLLM (VLLM_BASE_URL); rc-kg is started if RC_KG_URL is unset:
backend/.venv/bin/python evals/prompt_ab.py variants/orchestrator-v2 [--suite e2e --suite routing]
```

It runs `evals/run.py` twice -- baseline, then with `RC_AGENT_PROMPTS`
pointing at the variant -- and writes `traces/$USER/evals/<time>-prompt-ab-<variant>/`:
`comparison.md` (every metric for both, the change, the cases that flipped
pass/fail), `baseline.json`, `variant.json`. The default suite is `e2e`.
"Adopt a variant only if it wins on the metric it targets without losing
elsewhere; then copy its files into prompts/". The new versions then show in
every trace.

Only the agents' prompts (`app/agents/prompts/`) can be varied this way; the
writer's prompt is code in `app/prompts.py`.

### Context

The context budgets (`KG_CONTEXT_MAX_CHARS`, `RANK_CAPS`, `TICKET_MAX_TOKENS`)
are settings and constants (chapter 16). Their effect shows in the `rag`
suite's `context_chars_median`, the run log's `sources_packed` and
`prompt_breakdown`, and the `e2e` suite's tokens.

## 17.11 The release gate: `tests/run_all.sh`

```bash
cd ticketmate
tests/run_all.sh            # everything that needs no GPU (~3 min, most of it rc-kg starting)
tests/run_all.sh --quick    # stages 1-10 (seconds)
```

Exit 0 only if every stage passes; a failing stage prints up to 15 lines of
its output matching `FAIL|Error|error`. Run it "before publishing a release
(ood-apps/publish.sh) and after any change to prompts, skills or tools".

| # | stage | command | `--quick` |
|---|---|---|---|
| 1 | ticket context | `backend/.venv/bin/python tests/ticket_context/test_ticket_context.py` | yes |
| 2 | cluster lookups | `backend/.venv/bin/python tests/lookups/test_lookups.py` | yes |
| 3 | skills (playbooks) | `backend/.venv/bin/python tests/playbooks/test_playbooks.py` | yes |
| 4 | agent graph | `backend/.venv/bin/python tests/agent/test_agent.py` | yes |
| 5 | OpenTelemetry export | `backend/.venv/bin/python tests/agent/test_otel.py` | yes |
| 6 | guardrails | `backend/.venv/bin/python tests/agent/test_guardrails.py` | yes |
| 7 | prompt lint | `backend/.venv/bin/python tests/agent/test_prompts.py` | yes |
| 8 | eval: routing+guardrails | `backend/.venv/bin/python evals/run.py --suite routing --suite guardrails --gate` | yes |
| 9 | rc-kg unit tests | `rc-kg/.venv/bin/python -m pytest -q rc-kg/tests` | yes |
| 10 | rc-kg same as TicketMate's | `diff -rq` of `rc-kg/` against `/projects/rc/projects/ticketmate/rc-kg`, excluding `__pycache__`, `.venv`, `.venv-eval`, `vendor`, `data`, `.env`, `.pytest_cache` | yes |
| 11 | rc-kg ticket regressions | `cd rc-kg && scripts/with_kg.sh -m eval.ticket_cases` | no |
| 12 | eval: RAG on ground truth | `backend/.venv/bin/python evals/run.py --suite rag --gate` | no |
| 13 | end-to-end contract (A-G) | `tests/rc_kg/run_contract.sh` | no |

Stage 10 exists because "both apps must run the same knowledge-graph code
(they publish to one shared store)".

`tests/agent/test_agent.py` (stage 4) uses fakes for the model and rc-kg,
and checks:

- **tools**: 12 distinct, none named like an action; scoping (the request
  agent has no `lookup_job`); anchored patterns and closed objects;
  `load_skill` offers exactly the procedure skills; hostile and out-of-scope
  calls refused with nothing run; a repeat served from cache; a search
  merged into the sources;
- **ReAct**: the plan runs first without tools; the path; the reflection
  catches a named job not looked up; budgets hold; a server without tool
  calls stops cleanly;
- **orchestrator**: each rule, the model, the default;
- **critic** and **reviewer**: each rule; an unparseable verdict never
  blocks;
- **the whole graph**: route, path with a revision, the writer drafting from
  the agent's evidence, the revised draft as the result, provenance, a
  follow-up skipping the specialists, bounded revisions, a draft still
  written without tool calls.

The contract (stage 13) starts stub model servers (`tests/rc_kg/stub_vllm.py`),
a real rc-kg (`scripts/serve_kg.sh`) and, for the failure stages, a fake or
unreachable one, and runs backends over HTTP. TicketMate's stages A-E (the
real graph; an rc-kg claiming API v2.0; rc-kg unreachable; the website-only
variant; a model server that rejects `json_schema`) plus:

| stage | checks |
|---|---|
| F | the stub orchestrator routes to `incident` (`model: ...`); the agent plans and finishes; its tool calls (`search_knowledge_graph`, then `lookup_job` with `1; touch /tmp/stub_pwned`) are in the run log; the hostile one is refused and `/tmp/stub_pwned` does not exist; critic and reviewer both reviewed; the path; the manifest; every node, tool and review is a run; the draft is grounded; the panel gets the tool names |
| G | the stub with `STUB_NO_TOOLS=1` refuses tools as vLLM does without `--enable-auto-tool-choice`: `stopped: tools unsupported`, and the draft is still produced and grounded |

The `e2e` suite and `prompt_ab.py` need a GPU and are not in the gate: run
them on a GPU job before adopting a change that affects the model's
behaviour.
