---
name: "19. Sign-off, request forms, the LLM judge, readable traces"
route: /quality-loop
menu: Operations
---

# 19. Sign-off, request forms, the LLM judge, readable traces

Four features that work together on every draft. All four are in both
repositories; where rc-agent differs, a box says so.

| feature | what the engineer sees | code |
|---|---|---|
| Sign-off | the reply ends with *their* sign-off, exactly as typed in the popup | `extension/popup.*`, `evaluators.signoff_text` / `apply_signoff` |
| Request-type extraction | a catalog form's answers fill the playbook's fields (multi-GPU, long partition, access, course, ...) | `playbooks.form_answers`, `form.<field>` header lines |
| LLM judge | a "LLM judge" section in the panel; a flagged draft is rewritten once and, if still low, gets a *Check before sending* line | `evaluators.judge`, `main.draft`, `content.js judgeSection` |
| Readable traces | beside every run log `<run>.json`, a `<run>.md` a person reads top to bottom | `trace_report.py`, `runlog.write_report`, `cluster/traces.py md` |

## 19.1 The engineer's sign-off

**Where it is set.** The toolbar popup has a *Sign the reply with* text
area (up to 300 characters). It is stored in `chrome.storage.local` under its
own key, `rcSignoff`, so saving or auto-configuring the connection never
touches it. Empty means the team sign-off.

**How it travels.** `content.js` reads `rcSignoff` when *Draft reply* is
clicked and puts it in the `DRAFT` message; `background.js` sends it as
`signoff` in `POST /api/draft` (`DraftRequest.signoff`, max 300 characters).

**How it is applied -- in code, not by the model.** The model is still asked
for the default `Best regards,\nResearch Computing`: a 7B model copies a fixed
string reliably, while a person's name it might "improve" or drop. After
citations (so the References block stays above it),
`evaluators.apply_signoff` replaces the *last* occurrence of the default with
the engineer's text. `signoff_text` tidies it first: CRLF to LF, trailing
spaces off each line, outer blank lines off, at most `MAX_SIGNOFF_LINES` (6)
lines; otherwise it is used exactly as typed.

**Checked like every other rule.** `assert_signoff` checks the reply ends with
*that* sign-off; `metrics` and the judge strip it before measuring the body.

> **rc-agent:** the sign-off is applied after the output guardrails, so a
> guardrail never sees or changes the engineer's name.

## 19.2 Request-type extraction from the request form

Chapter 3.4 explains how the extension reads a catalog item's form and sends
it as `Request form:` with one `- question: answer` per line; chapter 5 how
the form is kept out of cleaning. This section is what the playbooks do with it.

**Each request type names its own questions.** Two optional header lines:

```
form.username: (?i)\buser ?name\b       a request-form question -> a field
rubric: The reply does not grant access before RC has verified a benchmark.
```

`Playbook.extract` now fills each field in this order:

1. If the playbook has `form.<field>` and the form has a question matching it,
   the **answer** is the value. When there is also a `field.<field>` pattern,
   it only picks the value out of the answer (`doe.l` out of
   `doe.l (Lee Doe)`); if it finds nothing, the whole answer is the value.
2. Otherwise the `field.<field>` pattern runs over the whole cleaned ticket,
   as before.

So a username comes from the form's *Username* answer, not from every
`first.last` in the thread (a CC'd colleague, an RC staff address). A field
may be form-only (no `field.*` line): its value is the whole answer, and a
`require.<field>` on it asks the researcher when the form did not have it.
`require.*` may now name a `form.*` field.

`playbooks.form_answers(text)` parses the block: it starts at a line
`Request form:`, ends at `Work notes / activity` or a forwarded message, and
a line not starting `- ` continues the previous answer (text areas).

**Traceability.** `Match.sources` records, per field, `"form: <question>"` or
`"text"`; it is in the run log (`playbooks[].value_sources`), on the readable
page, and the prompt marks form values *(the researcher's answer on the
request form)*.

**Telling the forms apart.** The question text is part of the retrieval query
(it is the researcher's form), so a question two forms share must not decide
the playbook: the multi-GPU and long-partition forms both ask *"Describe your
benchmarking calculation in detail"*. Playbooks use `unless` lines for this
(`tests/playbooks/test_playbooks.py` checks each form type matches exactly one).

**Writing them.** Keep `form.*` patterns loose (`(?i)\buser ?name\b`), since
ServiceNow's labels change; check them against a real task with the panel's
*request form (N answers)* row and the run log's `value_sources`.

## 19.3 The LLM judge

The judge is the serving model asked, with `JUDGE_SYSTEM`, to grade a draft
against the ticket, the sources the writer had, and a **rubric**.

**What it grades** (`evaluators.judge`, temperature 0, JSON mode when the
server has it, `judge_timeout_seconds`):

| key | meaning | flagged when |
|---|---|---|
| `hallucination` | share of factual claims supported by neither sources nor ticket | > `judge_max_hallucination` (0.3) |
| `correctness` | would following it resolve the request | < `judge_min_correctness` (0.5) |
| `conciseness` | no padding | never (shown only) |
| `answer_relevance` | addresses what was asked | never (shown only) |
| `rubric[]` | each rubric line: `met` true / false / null (not applicable) | any `false` |

**The rubric is per request type** (`playbooks.rubric_items`): for each
matched playbook, *"It follows RC's procedure for <title>, at the stage the
ticket is at"*, then the playbook's own `rubric:` lines, then *"It asks the
researcher for X"* for each missing requirement. A multi-GPU reply is graded
on what a multi-GPU reply must do; a ticket with no playbook has no rubric.

**Modes** (`JUDGE_MODE`, `evaluators.judge_mode`):

| mode | when the judge runs | where grades go |
|---|---|---|
| `gate` (default) | before the draft is returned | the response (`judge`), the run tree's feedback, the panel |
| `background` | after the draft is returned, sampled by `ONLINE_EVAL_SAMPLE_RATE` | `<run>.feedback.jsonl` |
| `off` (or `ONLINE_EVAL=false`) | never | -- |

**The gate, step by step** (`main.draft`):

1. The draft is finished as usual -- citations, sign-off, assertions and
   metrics (`finish()`).
2. `run_judge(response, "draft")`; an `llm_judge` run (type `llm`) in the run tree.
3. If the verdict is `low` and `JUDGE_REWRITE` is on: the writer is called
   **once** more with the same evidence and `evaluators.revision_note(result)`
   appended to the engineer's instructions (*"A reviewer checked your previous
   draft ... fix these problems"*), the new draft is finished the same way and
   judged (`"rewrite"`), and `evaluators.better` keeps whichever has fewer
   flags (then the higher correctness minus hallucination). A rewrite that is
   not better is discarded, and its model calls are marked `discarded` so the
   token accounting's *final call* is the draft actually returned.
4. If the returned draft is still `low`, `evaluators.caveat` puts
   *"Quality check (LLM judge) -- review before sending: ..."* first in the
   caveats, so it shows in *Check before sending*.
5. The judge's tokens are counted apart (`tokens.judge`); every judge call is
   in `trace.judge_calls` with its phase, latency and usage.

`response.judge`: `{mode, verdict "ok"|"low"|"error", scores, rubric, reasons,
latency_ms, first, rewrite, rewritten}` -- `first` is the first draft's grades
when a rewrite was tried; `rewrite` is `kept`, `discarded: ...` or `failed: ...`.

**Cost.** One judge call per draft; a flagged draft adds one draft call and a
second judge call. The judge's output is short (a JSON grade), but its prompt
holds the ticket and sources, so expect each judge call to cost a large part
of a draft call; this has not been measured on a GPU yet (the run log's
`timings_ms.judge` and `tokens.judge` will show it). Set
`JUDGE_MODE=background` to take it off the critical path.

**What it is not.** The judge is the same model that wrote the draft: its
grades are a strong hint, and the panel says so. People's feedback and the
reviewed-ticket regression cases remain the ground truth.

> **rc-agent:** the judge runs after the agent graph's own critic and reviewer
> agent, on the finished draft (after citations and output guardrails). A
> rewrite goes back through `write_with` with the judge's reasons as
> `revision_notes` and the evidence the agents gathered; it is not another
> trip through the graph.

**In the panel** (`content.js judgeSection`): *LLM judge: looks right* or
*review before sending*, *(rewritten once)* when it was; the four grades as
percentages with the judge's reason, then *For this kind of request:* each
rubric item that applies, ✓ or ✗ with why. Not shown in background mode.

## 19.4 Readable traces

Every draft now has two records side by side in `traces/$USER/runs/<ticket>/`:

| file | for | contents |
|---|---|---|
| `<time>-<id>.json` | tools, audits, the evals | the full trace: every run with inputs/outputs, the exact prompt of every call, tokens, timings, scores (chapter 10) |
| `<time>-<id>.md` | people | the same draft, in reading order |
| `<time>-<id>.feedback.jsonl` | both | feedback added later (people, the background judge) |

The page (`trace_report.render`) has: a summary table (when, outcome,
model and strategy, confidence, latency, tokens, node and job, versions); **The
reply**; **For the engineer** (summary, steps, *check before sending*, cited
pages); **Quality** (assertions PASS/FAIL, metrics, the LLM judge with its
rubric and whether it rewrote, feedback); **How it was made** (stage timings,
ticket cleaning, the engineer's instructions, cluster lookups, playbooks with
where each value came from, retrieval sources with scores, citations, every
model call with its phase, judge calls, the run tree as an outline); and the
**exact prompt and output**, folded in `<details>`.

The JSON is the source of truth; the page is derived. `runlog.record` writes
both; `runlog.add_feedback` rewrites the page so feedback appears on it. Both
are mode 600. `trace_report.py` uses only the standard library, so:

```bash
python3 ticketmate/cluster/traces.py md ab12cd34      # print one draft's page
python3 ticketmate/cluster/traces.py md --all         # (re)write the page beside every run log
```

## 19.5 Settings

| setting | default | meaning |
|---|---|---|
| `JUDGE_MODE` | `gate` | `gate`, `background` or `off` |
| `JUDGE_REWRITE` | `true` | in gate mode, rewrite a flagged draft once |
| `JUDGE_TIMEOUT_SECONDS` | `60` | per judge call |
| `JUDGE_MAX_HALLUCINATION` | `0.3` | flag above this share of unsupported claims |
| `JUDGE_MIN_CORRECTNESS` | `0.5` | flag below this |
| `ONLINE_EVAL` | `true` | `false` turns the judge off in every mode |
| `ONLINE_EVAL_SAMPLE_RATE` | `1.0` | background mode only |
| `signoff` (request field) | `""` | the engineer's sign-off; set in the popup |

## 19.6 Tests

| test | proves |
|---|---|
| `tests/playbooks/test_forms.py` | form parsing (multi-line answers), values from the form before the thread, value sources, form-only fields asked for, rubric items, header rules; self-contained (no `PLAYBOOK_DIR`) |
| `tests/playbooks/test_playbooks.py` (with `PLAYBOOK_DIR`) | long-partition, multi-GPU and course forms each match exactly their own playbook, the username from the form, rubric lines present |
| `tests/judge/test_judge.py` | sign-off tidying and replacement, the sign-off assertion, judge flags and thresholds, choosing the better draft, the revision note, the readable page's sections, old logs still rendering |
| `tests/ui/extension_result_test.py` | the popup's sign-off is sent with the draft; the judge section's scores, applicable rubric items and rewrite note |
| `tests/rc_kg/run_contract.sh` A, E | gate: grades in the response and run tree, judge tokens counted apart, the `.md` page (mode 600, rewritten on feedback); a flagged draft rewritten and the better one returned, both drafts and judge calls in the trace by phase; the engineer's sign-off exactly, below References. E: background mode records grades beside the run and none in the response |

The stub model server (`tests/rc_kg/stub_vllm.py`) answers the judge with
every rubric item met, except on a ticket containing `JUDGE-LOW`, whose first
draft it grades low until the rewrite comes back marked `(revised)`.
