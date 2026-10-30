---
name: 10. Citations, evaluation, tracing
route: /citations-evaluation-tracing
menu: Knowledge
---

# 10. Citations, evaluation, tracing

> **New:** Since version 0.9.9 the LLM judge grades before the draft is returned by default (`JUDGE_MODE=gate`), with a per-request-type rubric and one rewrite, and every run log has a readable `.md` beside it: chapter 19.

After the model answers, three things happen to every draft: its `[S#]`
markers become links that are safe to send, code checks it against the
prompt's rules, and everything about it is recorded.

| file | role |
|---|---|
| `backend/app/kg_client.py` | `apply_citations`, `strip_citations` |
| `rc-kg/retrieval/citations.py`, `rc-kg/retrieval/support.py` | the server side of `/cite`: rendering, URL checks, the support check |
| `backend/app/evaluators.py` | assertions, metrics, answer relevance, the background LLM judge |
| `backend/app/tracing.py` | the run tree of each draft |
| `backend/app/runlog.py` | the run log: one JSON file per draft, and its feedback |
| `backend/app/ops_store.py` | where every trace, metric and eval report is kept |
| `backend/app/otel.py` | OpenTelemetry spans: to a file, and to a collector |
| `ops/ops_report.py`, `cluster/traces.py` | reading it all back |

## 10.1 From `[S#]` to links

The model was told to cite sources as `[S1]` or `[S1, S3]` and never to write
a URL (chapter 8). `main.draft` then calls:

| `kg.status` | what happens |
|---|---|
| `ok` | `kg_client.apply_citations(draft, kg, request_id, extra_allowed_urls=<playbook links>)` |
| `unavailable` or `disabled` | `kg_client.strip_citations(draft)`: every `[S#]` marker is removed from every field, since a model can emit them without sources and a raw `[S3]` must not reach a researcher. `unavailable` also adds the caveat "The RC knowledge graph was unavailable (...), so this draft was not checked against the RC website and has no links." |

`apply_citations` (an `rc_kg.cite` run, type `tool`) sends **every field in
one `/cite` call**, so the reply and the steps share one numbering:

```python
texts = [draft_response, problem_summary, *suggested_steps, *caveats]   # reply first
POST /cite {"texts": texts,
            "refs": {"S1": <url_with_anchor>, ...},                      # from kg.sources
            "allowed_urls": kg.allowed_urls + playbook links,
            "style": "numbered", "check_support": true}
```

The reply goes first so the researcher's references read 1, 2, 3 in order.

### What rc-kg does (`retrieval/citations.py`, `render_many`)

1. **URLs the model wrote, before any marker is expanded.** Each
   `https?://...` (trailing `.,;:!?*_` kept aside):
   - in `allowed_urls` (compared lower-cased, without a trailing slash): kept,
     in the allowed spelling;
   - same page as an allowed URL but an invented fragment: **cut back to the
     page**;
   - otherwise: replaced by `[link removed]` and listed in `removed_urls`.
2. **Markers.** Each ref in a marker is looked up in `refs`. Known refs are
   numbered by first use across all texts (`[1]`, `[2]`...) and listed in
   `references` as `{n, ref, url}`; unknown refs go to `unknown_refs` and are
   dropped; a marker with no known ref disappears with its leading space.
   (Other styles: `markdown` gives `[S1](url)`, `plain` gives `[S1] <url>`.)
3. **Support check** (`check_support`, once rc-kg is ready): verdicts on the
   **original** texts, where each marker still marks its claim (10.2).

### What the backend does with the answer

| result | becomes |
|---|---|
| `references` | `response.references` (`{n, title, url}`); title = page title, plus ` - <heading>` unless the heading is `Overview` or the page title |
| a reference block | `References:\n[1] <title>: <url>\n...` inserted **above** the sign-off `Best regards,\nResearch Computing` (or appended if the sign-off is missing) |
| a sources step | `Sources: [1] <url>; [2] <url>` appended to `suggested_steps`, because steps go to work notes, which may not include the reply |
| a support verdict other than `ok` | a caveat: `Check citation [n] on "<claim, at most 120 chars>": the page "<title>" (<url>) matches it better.` for `better_source`, or `... the cited page does not clearly say this.` for `weak`. Advisory: the link is left as the model chose it |
| `removed_urls` | a caveat: `Removed N link(s) the model wrote that were not among the RC pages it was given: ...` |
| `/cite` fails | markers stripped, no references; trace `status: failed`, `markers_stripped: true` |

If retrieval was low confidence and nothing was cited, a caveat says "No RC
website page closely matched this ticket; the reply is not backed by RC
documentation."

The cite trace (in the run log as `citations`): `status`, `latency_ms`,
`cited`, `references`, `removed_urls`, `unknown_refs`, `support`,
`caveats_added`, and rc-kg's own `trace`.

## 10.2 The support check

`rc-kg/retrieval/support.py`. Every link already came from retrieval; this
checks that `[S2]` is attached to a sentence S2 actually says.

1. **Claims.** For each marker, the claim is the text back to the previous
   sentence end, newline or marker (`extract_claims`).
2. **Scores.** The claim is embedded (as a query). Against each retrieved
   section: `total = cosine + 0.3 x lexical`, where lexical is the share of
   the claim's distinctive tokens (3+ characters, not stopwords) found in the
   section's heading and text -- what embeddings blur (`seff`, `/scratch`,
   partition names).
3. **The cited page** is credited with its best retrieved section; sections
   on the same page are not rivals. Rivals are sections on other pages that
   the claim did not cite.
4. **Verdict:**

| verdict | when |
|---|---|
| `better_source` | the best rival beats the cited page by `>= MARGIN` (0.10) on `total` **and** on cosine alone, and the cited page's `total` is below `CONFIDENT` (0.95) |
| `weak` | otherwise, the cited page's `total` is below `FLOOR` (0.55) |
| `ok` | otherwise |

Why relative and not a threshold: an absolute cosine separates supporting
from non-supporting sections poorly (AUC 0.76), because similarity levels
differ a lot between pages. Both signals must agree because term overlap
alone flagged correct citations of pages that mention each other's terms.
`CONFIDENT` exists because, with two sites covering the same ground, a
correct citation of one was flagged when the other scored higher. The
comments in `support.py` record each measurement; `eval/citation_check.py`
re-measures it, and the README holds the current numbers. Its known
weakness: it can only point to a better source among those retrieved.

## 10.3 Per-draft evaluation

Run in `main.draft` inside an `evaluate` run, right after citations, so the
panel can show failures. Results go into `response.evaluations`
(`assertions`, `assertions_failed`, `metrics`) and onto the trace as
feedback.

### Assertions

`evaluators.assertions(response, ticket_text, kg_status, kg_low_confidence,
cite_trace, instructed)` returns `{name: passed}`. Each is a rule the system
prompt states, checked in code:

| assertion | passes when |
|---|---|
| `assert_signoff` | the reply ends with `Best regards,\nResearch Computing` |
| `assert_no_placeholder` | no bracketed name/signature placeholder (`[Name]`, `[Your Name]`, `[insert name]`, `[the engineer]`, `[signature]`, ...), no `<your name>`, no `XXXX` |
| `assert_no_invented_ticket_number` | every ServiceNow-style number in the reply (`SCTASK`, `RITM`, `INC`, `TASK`, `CHG`, `PRB`, `REQ`, `CS` + 5 or more digits) appears in the raw ticket text |
| `assert_no_promises` | no "within N hours/days/weeks", "by tomorrow", "by end of day/week", "we will increase/extend/grant/approve/raise your/the", "I will escalate", "we guarantee", "guaranteed" |
| `assert_confidence_consistent` | not (confidence >= 0.8 and a caveat says something is not provided, missing, unclear, unknown, ...) |
| `assert_summary_short` | `problem_summary` is at most 2 sentences |
| `assert_has_steps` | at least one step |
| `assert_urls_from_sources` | the citation step removed no URL |
| `assert_no_unknown_citations` | no `[S#]` named a missing source |
| `assert_cites_when_grounded` | only when grounding was `ok`, not low confidence, and the engineer gave **no** instructions (a follow-up has nothing to cite): at least one reference |

A failed assertion is listed in `assertions_failed`, shown in the panel, and
tags the trace `assertion_failed`.

### Metrics

`evaluators.metrics(response, cite_trace)`, no LLM. The reply body excludes
the References block and the sign-off.

| metric | value | better |
|---|---|---|
| `reply_words` | words in the body | lower |
| `conciseness` | 1.0 up to `CONCISE_TARGET_WORDS` (180), falling linearly to 0 at `CONCISE_LIMIT_WORDS` (450) -- starting points, to tune against what engineers send | higher |
| `hallucinated_urls` | URLs the citation step removed | lower |
| `unsupported_citation_rate` | share of support verdicts not `ok` (only when there were verdicts) | lower |
| `answer_relevance_embedding` | cosine of the ticket (as a query) and the reply body (as a passage) via rc-kg `/similarity`; absent without rc-kg | higher |

`evaluators.lower_is_better(key)` encodes the direction for `hallucination`,
`hallucinated_urls`, `unsupported_citation_rate` and `reply_words`.

### Automatic feedback on the trace

Besides each assertion (source `assertion`, 1/0) and metric (source
`metric`), `main.draft` attaches: `assertions_passed` (share), `grounded`
(kg ok), `references` (count), `citations_flagged` (caveats starting "Check
citation"), `urls_removed`, `retrieval_low_confidence`, `needed_fallback`
(more than one model call).

### The LLM judge

After the response is built and the run log written,
`evaluators.schedule_judge` starts `judge_and_record` as a background
`asyncio` task (a reference is held until it finishes). It never delays a
draft.

| | |
|---|---|
| when | `ONLINE_EVAL=true` (default) and `random() < ONLINE_EVAL_SAMPLE_RATE` (default 1.0) |
| model | the serving model, temperature 0.0, `max_tokens` 600 |
| format | `response_format={"type": "json_object"}`; if the server refuses, the same call without it; parsed with `extract_json` |
| input | `JUDGE_SYSTEM`, then the ticket, the sources the writer saw (`(none)` if left out), the reply body and the steps |
| output | `hallucination` (share of factual claims supported by neither sources nor ticket; lower is better, with up to 5 quoted unsupported claims), `correctness`, `conciseness`, `answer_relevance`, each 0-1 |
| stored | one feedback record per key beside the run log, `source: llm_judge`, with `lower_is_better`, the reason, and the judge's model, latency and usage |
| on error | one `llm_judge_error` record; never raised |

**The judge is the same model that wrote the draft.** Treat its scores as a
trend, not ground truth; people's feedback is the ground truth.

### People's feedback

`POST /api/feedback` (session required), body `FeedbackRequest`:

| field | constraint |
|---|---|
| `request_id` | 8 hex characters |
| `key` | `^[a-z][a-z0-9_]{0,39}$`; the panel sends `thumbs` (1/0), `correctness` (1/0.5/0), `usage` (1/0.5/0 with value `used_as_is`/`edited`/`not_used`) |
| `score` | -1..1 or none |
| `value`, `comment` | up to 40 / 2,000 characters |
| `correction` | the reply as actually sent, up to 50,000 |

`runlog.add_feedback` appends the record (with id, time, `$USER`, source
`user`) to `<run>.feedback.jsonl`; 404 if no run log has that id. A
correction also adds a derived `edit_similarity` record: the `difflib` ratio
of drafted and sent reply (1 = sent unchanged).

## 10.4 Run trees

`tracing.py` keeps LangSmith's data model, locally. A **trace** is one draft;
it is a tree of **runs**.

```
draft                                   chain       root
├── ticket_context.prepare              tool
├── cluster.lookups                     tool        read_only: true
├── rc_kg.retrieve                      retriever   documents + scores
│   ├── rc_kg.embed_query               embedding   derived from rc-kg's timings
│   ├── rc_kg.vector_search             retriever   derived
│   ├── rc_kg.fulltext_search           retriever   derived
│   ├── rc_kg.topic_boost               tool        derived
│   └── rc_kg.graph_expand              tool        derived
├── playbooks.match                     tool
├── generate_draft                      chain
│   ├── llm  (one per model call)       llm         messages, params, tokens
│   └── parse_draft (one per output)    parser
├── tokenize.prompt_breakdown           tool
├── rc_kg.cite                          tool        references, verdicts
└── evaluate                            chain       assertions, metrics
```

| concept | in the code |
|---|---|
| run types | `chain`, `retriever`, `embedding`, `llm`, `parser`, `tool` (`RUN_TYPES`, asserted) |
| a run | `id`, `parent_id`, `name`, `run_type`, `start_time`, `end_time`, `latency_ms`, `status` (`running`/`success`/`error`), `error`, `inputs`, `outputs`, `metadata`, `tags`, `usage` |
| opening a run | `with tracing.run(name, type, inputs, metadata, tags) as r:` -- a child of whatever run is current; an exception ends it with the error and re-raises. **Outside a trace it yields `None`**, so instrumented code also works untraced (hence the `if run is not None` checks) |
| the current run | a `contextvars.ContextVar`, so code deep in the stack (`llm_client`) opens child runs without the trace being passed down; asyncio tasks and `to_thread()` inherit it |
| derived runs | `add_derived(parent, ...)`: completed child runs rebuilt from timings measured elsewhere (rc-kg's stages, which run in sequence), placed inside the client-side latency and marked `derived: true` |
| finishing | `Trace.finish()` ends the root and closes any run an exception left open (`did not finish`) |
| feedback | `Trace.add_feedback(key, score, comment, source, run_id)`: attached to a run (the root by default) |

The root run's metadata groups traces like LangSmith's projects, sessions and
threads:

| key | value |
|---|---|
| `request_id` | 8 hex digits; also returned to the panel and sent to rc-kg as `X-Request-ID` |
| `thread_id` | the ticket folder name (all drafts for one ticket share it) |
| `session_id` | `$SLURM_JOB_ID`, else `local` |
| `project` | `rc-copilot/<graph variant>` (`no-kg` without a graph) |
| `node`, `gpu`, `model` | host, `nvidia-smi` GPU names, configured model |
| `prompt_version` | chapter 8.3 |
| `versions` | `ticket_mate`: this checkout's commit (`-dirty` with local changes under `backend`) and the OOD release (`RC_COPILOT_RELEASE`); `rc_kg`: API version, snapshot, retrieval trace |

Root tags make traces filterable: `rc-copilot`, `kg:<status>`,
`lookups:<n>`, `playbook:<name>`, `playbook:missing_info`,
`kg:sources_not_needed`, `ticket_trimmed`, `assertion_failed`,
`model:<name>`.

## 10.5 Run logs

`runlog.record(...)` writes one JSON file per draft -- **failed drafts
included**, since a draft that never arrived is worth explaining:

```
traces/$USER/runs/<TICKET>/<YYYYmmdd-HHMMSS>-<request_id>.json
traces/$USER/runs/<TICKET>/<YYYYmmdd-HHMMSS>-<request_id>.feedback.jsonl
```

`<TICKET>` is `ticket_folder(ticket_number, text)`: the number the panel
sent, else the first ServiceNow number in the text, else `unknown-ticket`;
reduced to `[A-Za-z0-9_-]`, upper-cased, at most 64 characters, because the
value comes from a web page and must not escape the directory.

| top-level field | |
|---|---|
| `request_id`, `ticket_number`, `at`, `node`, `slurm_job_id`, `model`, `latency_ms` | |
| `strategy` | the structured-output strategy that worked (prompt-only output is more likely ragged) |
| `input` | `ticket_text` (raw, as scraped), `extra_instructions`, `chars` |
| `prompt` | the exact messages of the final call |
| `raw_response`, `draft`, `error` | |
| `trace_version` | 3 (= adds `run_tree`); the older top-level fields stay so readers of version-1 logs keep working |
| `trace` | below |

| `trace` key | |
|---|---|
| `request_id`, `started_at`, `versions` | |
| `ticket_context` | cleaning and fitting stats, context window, budget, playbook names |
| `retrieval` | the `KGContext` trace: status, k, latency, query topics, low confidence, sources (no text), rc-kg's own trace |
| `cluster_lookups`, `playbooks` | |
| `llm` | `params`, `final_strategy`, `attempts` (chapter 8.5) |
| `tokens` | chapter 8.6 |
| `citations` | 10.1 |
| `output` | `model_draft` (before citations) and `response` (what the panel got) |
| `timings_ms` | `retrieve`, `lookups`, `llm`, `citations`, `total` |
| `error` | |
| `run_tree` | `Trace.to_dict()`: `trace_id`, `root_run_id`, `runs`, `feedback` |

Recording is best-effort: an `OSError` returns `None` and the draft is still
returned. Only a recorded draft gets the background judge.

## 10.6 Where traces live, and who can read them

`ops_store.py`: everything is kept **in the repository**, per user, beside
the code -- on `/projects`, so it survives `/scratch` purges. A member who
cannot write the repository (the shared release belongs to its maintainer)
gets the same layout under `/scratch/$USER/rc_copilot_traces` instead (`ops_store.root()` tries the
repository once per process and falls back), rather than no traces at all.

```
<repo>/traces/$USER/
  runs/<ticket>/<time>-<id>.json         run logs (+ .feedback.jsonl)
  spans/YYYY-MM-DD.jsonl                 OpenTelemetry spans of every draft
  metrics/YYYY-MM-DD.jsonl               one row per draft
  evals/<YYYYmmdd-HHMMSS>-<suite>/       evaluation and ops reports
```

(`<repo>` is the checkout root: `/projects/rc/projects/ticketmate` or
`/projects/rc/projects/rc-agent`.)

| control | how |
|---|---|
| permissions | `traces/$USER/` and every subdirectory are forced to **0700**, every file is created **0600** (`os.open(..., 0o600)`), ticket folders 0700 too: ticket text can contain researchers' personal data, and `/projects` is group-readable |
| git | `/traces/` is in the repository's `.gitignore` |
| moving it | `RC_TRACES_DIR` moves the whole tree; `RUN_LOG_DIR` moves the run logs alone; `RUN_LOG_DIR=""` turns run logs off |
| failure | best-effort: a full disk never breaks a draft |

**The metrics row** (`ops_store.append("metrics", ...)`) has **no ticket
text**: `ts`, `request_id`, `ticket` (folder name), `model`, `mode`
(`pipeline`), `kg_status`, `sources`, `low_confidence`, `references`,
`urls_removed`, `playbooks`, `lookups`, `assertions_failed`, `confidence`,
`latency_ms`, `timings_ms`, `tokens`, `prompt_version`.

## 10.7 OpenTelemetry

`otel.export(tr.to_dict())` runs once per draft, as the trace is finished,
and never raises.

| exporter | when | how |
|---|---|---|
| file | unless `RC_TRACES_SPANS` is `0`/`off`/`false`/`no` | each span as OTLP-style JSON to `traces/$USER/spans/<date>.jsonl` (`SimpleSpanProcessor`) |
| OTLP/HTTP | `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` set | any collector (Jaeger, Tempo, Phoenix, Langfuse, Datadog) (`BatchSpanProcessor`) |

The tracer is configured on first use. Resource: `service.name` =
`OTEL_SERVICE_NAME` (default `rc-agent`), `service.namespace` =
`research-computing`.

- One trace per draft, **one span per run**, with the run's real start and
  end times and parent (runs are created parents first, ended children
  first).
- Attributes: `rc.run_id`, `rc.run_type`, `rc.status`, `rc.request_id`,
  `rc.thread_id`, `rc.project`, `rc.tags`, `rc.latency_ms`, and every scalar
  metadata value as `rc.meta.<key>` (strings cut to 500 characters).
- GenAI semantic conventions where they apply: on `llm` runs
  `gen_ai.operation.name = chat`, `gen_ai.system`, `gen_ai.request.model`,
  `gen_ai.usage.input_tokens` / `output_tokens`; on `tool` runs named
  `tool.<name>`, `gen_ai.operation.name = execute_tool` and
  `gen_ai.tool.name`.
- Error runs get an `ERROR` status. Feedback scores become `feedback` events
  on the run they rate.
- **Content is left out** (ticket text, prompts, drafts) unless
  `OTEL_EXPORT_CONTENT=true`, which adds `rc.inputs`/`rc.outputs` as JSON cut
  to 4,000 characters: a collector is usually shared.

## 10.8 Reading it back

**One draft, or many: `cluster/traces.py`.**

```bash
python3 ticketmate/cluster/traces.py --dir traces/$USER/runs list
python3 ticketmate/cluster/traces.py --dir traces/$USER/runs show <request_id> --prompt
python3 ticketmate/cluster/traces.py --dir traces/$USER/runs runs --type llm --status error
python3 ticketmate/cluster/traces.py --dir traces/$USER/runs stats --by prompt_version
python3 ticketmate/cluster/traces.py --dir traces/$USER/runs feedback <request_id> --key thumbs --score 1
python3 ticketmate/cluster/traces.py --dir traces/$USER/runs export drafts.csv   # no ticket text
```

The default directory is `RUN_LOG_DIR`, else `RC_TRACES_DIR/runs`, else
`traces/$USER/runs` as above when it exists, else the `/scratch` fallback's
`runs/`. Logs written before the move to `traces/` are in
`/scratch/$USER/rc_copilot_runs`; for those, pass `--dir`.

**The service over time: `ops/ops_report.py`.**

```bash
ticketmate/backend/.venv/bin/python ticketmate/ops/ops_report.py              # last 7 days, your traces
ticketmate/backend/.venv/bin/python ticketmate/ops/ops_report.py --days 30 --user all
```

It reads the metrics rows and prints a table: volume (drafts, tickets),
latency p50/p95 and mean time by stage, tokens per draft, knowledge-graph
health (ok rate, low-confidence rate, URLs removed), skills used, the
assertions that failed most, models and prompt versions -- plus the agent
rows of 10.9. It also writes it to `traces/$USER/evals/<time>-ops/ops.md`.
`--user all` reads every user's `traces/` directory; since each is 0700, in
practice it reads only those you have permission for.

**Feedback over time** is in the `.feedback.jsonl` files and the run trees;
`traces.py stats` summarises it per step and per group.

> **rc-agent:** the same files and directories, plus:
>
> - every agent step is a run: `graph`, `graph.orchestrator`,
>   `agent.<incident|request>`, `react.<agent>.plan` / `.reflect`, each tool
>   call as `tool.<name>` (so it gets the GenAI `execute_tool` attributes),
>   `graph.write`, `graph.review`, `review.reviewer`, and
>   `guardrails.input` / `guardrails.output`;
> - root metadata `project: rc-agent/<variant>`, and `config` with the agent
>   and pipeline settings that shaped the draft; tags `route:<route>`,
>   `agent:<how it stopped>`, `agent_calls:<n>`, `revised`,
>   `guardrail:<rule>`;
> - the run log's `trace` adds `agent` (the graph's provenance: route and
>   reason, plan, tool calls, reflections, reviews, the `manifest` of prompt,
>   skill and tool versions) and `guardrails` (every finding);
> - every guardrail finding is also a row in `traces/$USER/guardrails/`,
>   and the draft gets `guardrail_findings` feedback;
> - the metrics row has `mode: agent`, `route`, `route_reason`,
>   `agent_stopped`, `tool_calls`, `tool_refusals`, `reflections`,
>   `revisions`, `reviews`, `reviewer_supported_share`, `guardrails` and the
>   `manifest` sizes, which `ops_report.py` turns into the route mix, tool
>   use and refusals, review pass rate, revision rate, reviewer groundedness
>   and guardrails fired;
> - `evals/` holds the reports of the offline evaluations (chapter 17).
