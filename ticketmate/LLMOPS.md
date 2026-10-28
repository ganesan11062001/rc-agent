# rc-agent: LLMOps

How rc-agent is traced, evaluated, guarded and tuned. Architecture: [AGENT.md](AGENT.md).

## 1. Traces -- `rc-agent/traces/$USER/` (`backend/app/ops_store.py`)

```
traces/$USER/                     0700, files 0600 (tickets carry researchers' data); git-ignored
  runs/<ticket>/<time>-<id>.json    full run log: input, every prompt and completion, retrieval,
                                    lookups, agent provenance, guardrails, citations, evaluations,
                                    the LangSmith-style run tree (+ .feedback.jsonl: people, judge, metrics)
  spans/YYYY-MM-DD.jsonl            OpenTelemetry spans of every draft (app/otel.py)
  metrics/YYYY-MM-DD.jsonl          one row per draft, no ticket text
  guardrails/YYYY-MM-DD.jsonl       every guardrail finding with its request id
  evals/<time>-<suite>/             evaluation, optimization and ops reports
```

- **Every step is a run** in the draft's run tree: cleaning, input and
  output guardrails, lookups, retrieval (and rc-kg's stages), the graph and
  each node (`graph.orchestrator`, `agent.incident`, `react.incident.plan`,
  `agent.incident.step` model turns, `tool.<name>` calls, `graph.write`,
  `generate_draft`, `graph.review`, `review.reviewer`), citations, evaluators.
  Each has inputs, outputs, timing, status, token usage.
- **Provenance**: `trace.agent` -- the route and why, the plan, every tool
  call (arguments, ran or refused, result), reflections, every review, the
  revisions and the path through the graph.
- **Versions**: the root carries the prompt version, a config snapshot (no
  secrets), and the manifest -- the hash of every agent prompt, skill and
  tool schema. Two drafts can be compared knowing exactly what differed.
- **OpenTelemetry**: the same tree as spans, GenAI semantic conventions
  (`gen_ai.request.model`, `gen_ai.usage.input_tokens`/`output_tokens`,
  `gen_ai.tool.name`), always to `spans/`, and to any collector when
  `OTEL_EXPORTER_OTLP_ENDPOINT` is set (Jaeger, Tempo, Phoenix, Langfuse,
  Datadog). Content only with `OTEL_EXPORT_CONTENT=true`.
- **Feedback**: people's (thumbs, comments, the reply as sent -> edit
  similarity), the LLM judge's, assertions and metrics -- attached to the
  trace, as LangSmith does.

`ops/ops_report.py [--days 30] [--user all]`: volume, routes, latency p50/p95
by stage, tokens, tool use and refusals, review pass and revision rates,
reviewer groundedness, guardrails fired, knowledge-graph health, failing
assertions, live versions.

## 2. Guardrails (`backend/app/guardrails.py`)

| side | rule | action |
|---|---|---|
| input | chat-template control tokens (`<\|im_start\|>`, `[INST]`, `<<SYS>>`...) | removed |
| input | prompt injection ("ignore all previous instructions", "you are now", "reveal your system prompt"...) | flagged; every model is told the ticket is data, not instructions |
| output | credentials (private keys, AWS/GitHub tokens, bearer tokens, API keys, passwords) | **redacted**, always |
| output | destructive commands to the researcher (`rm -r` on /, ~, absolute paths; `chmod 777`; `sudo`; `mkfs`/`dd`; fork bomb; `scancel -u`) | sent back to the writer; a caveat if still there |
| output | internal work-note text repeated to the researcher | sent back; caveat |
| output | a timeline neither the ticket nor RC's procedures give | caveat |
| tools | arguments outside the strict patterns; unknown tools; out-of-scope tools | refused, nothing runs |
| writing | claims of actions nobody took; redirecting to RC; ignoring missing info; repeating the last reply; placeholders | critic -> revision |
| writing | claims the evidence does not support | reviewer -> revision |

Nothing is sent and nothing changes on the cluster: the engineer reviews
every draft, and no tool can write.

## 3. Ground truth and evaluation (`evals/`)

`evals/groundtruth.yaml`: 22 anonymised cases from tickets the team reviewed
(+ synthetic ones for procedures without real tickets yet), labelled with
route, skills, required lookups, expected and forbidden knowledge-graph
pages, low-confidence, what the reply must ask, reply must/must-not
patterns, guardrails. `evals/run.py` composes each into the exact
ServiceNow page and scores it:

| suite | needs | measures |
|---|---|---|
| `rag` | rc_kg (started if needed) | Hit@6, MRR, recall@6, forbidden pages, low-confidence precision/recall, context size |
| `routing` | -- | skill triggers; the orchestrator's rule routes |
| `guardrails` | -- | injection detection, false positives |
| `e2e` | vLLM | route accuracy, skill recall, lookup recall, cited hit, reply checks, forbidden claims, review pass, revisions, latency, tokens, similarity to reference |

`--gate` fails below the floors in `evals/run.py`; `--compare report.json`
shows the change from a baseline. Also: `rc-kg/eval/run.py` (45 questions,
the graph build gate), `rc-kg/eval/ticket_cases.py` (12 regressions),
`rc-kg/eval/retrieval_eval.py` (RAGAS context precision/recall),
`tests/agent/replay_eval.py` (re-draft real tickets from the run logs).

## 4. RAG optimization (`rc-kg/eval/optimize.py`)

Sweeps the retriever's settings (RRF k, fulltext weight, topic weight,
sources per page, stale factor) and ranks them on the 45 questions, the
12 regressions and the ground truth at once; a setting that fails a
regression or retrieves a forbidden page is disqualified. Results to
`traces/$USER/evals/rag-optimize-*.json`; adopt by hand, with the numbers.

## 5. Context engineering

The writer's prompt, in order: system rules; the ticket (cleaned, RC staff /
researcher labelled, fitted to what the window leaves); cluster lookups;
knowledge-graph sources (fitted by rank, `app/context_pack.py`); the skill's
runbook and what is missing; the engineer's instructions; revision notes;
the checklist last (a 7B model acts on what it read last). Budgets are
computed from the model's real window; follow-ups and closings drop the
sources; retrieval reads only the researcher's words; every agent sees what
is already gathered so nothing is fetched twice.

## 6. Prompt optimization

Prompts are versioned files; `tests/agent/test_prompts.py` lints them
(placeholders, length budgets, the safety rules stated). To change one:
write a variant directory, run `evals/prompt_ab.py <dir>` on a GPU job, adopt
it if it wins without losing elsewhere.

## 7. Release gate

`tests/run_all.sh` (`--quick` for unit tests only): ticket context, lookups,
skills, agent graph, OpenTelemetry, guardrails, prompt lint, routing and
guardrail evals, rc-kg unit tests, rc-kg regressions, RAG eval, end-to-end
contract (A-G). Run before every release and after any prompt, skill, tool
or retrieval change. The `e2e` eval and `prompt_ab.py` need a GPU: run them
on a GPU job before adopting a change that affects the model's behaviour.
