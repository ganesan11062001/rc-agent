---
name: 8. Prompts and generation
route: /prompts
menu: The pipeline
---

# 8. Prompts and generation

One model call writes the draft. This chapter covers what that call is sent
and how its answer becomes a validated draft.

| file | role |
|---|---|
| `backend/app/prompts.py` | every prompt string, and `build_messages` that assembles them |
| `backend/app/llm_client.py` | the vLLM client: structured output with fallbacks, parsing, errors, health, context window, token counts |
| `backend/app/models.py` | `LLMDraft` (what the model must return), `DRAFT_JSON_SCHEMA` (what guided decoding enforces), `DraftResponse` |
| `backend/app/main.py` | the token budget, the call, token accounting |

## 8.1 The draft JSON

The model returns exactly one JSON object with five keys. `models.LLMDraft`
validates it:

| key | type | constraint | meaning |
|---|---|---|---|
| `problem_summary` | string | | one or two sentences, for a colleague |
| `suggested_steps` | string[] | | diagnostic and resolution steps for the RC engineer, most likely cause first |
| `confidence` | number | `0.0 <= x <= 1.0` (Pydantic `Field(ge=0, le=1)`) | see 8.3 |
| `caveats` | string[] | | what the engineer must verify before sending |
| `draft_response` | string | | the reply to the researcher |

`confidence_label` is **deliberately not** in the model's output. The server
derives it (`models.confidence_label`): `>= 0.8` high (`HIGH_CONFIDENCE`),
`>= 0.6` medium (`MEDIUM_CONFIDENCE`), else low. The thresholds are enforced
by code, not by the model; the frontend mirrors them.

`DraftResponse` extends `LLMDraft` with what the backend adds:
`confidence_label`, `model`, `latency_ms`, `references`, `kg_status`,
`context`, `lookups`, `request_id`, `usage`, `evaluations`.

`DRAFT_JSON_SCHEMA` is the same five keys as a JSON Schema, **written by hand**
rather than generated from `LLMDraft`: OpenAI's `strict` json_schema mode
needs `additionalProperties: false` and every key required, and a small
schema keeps vLLM's grammar compiler happy. If you add a field to `LLMDraft`,
add it to the schema, the system prompt and the extension.

The request side (`DraftRequest`) limits what can reach the prompt:
`ticket_text` 1-200,000 characters (raw page text; it is cleaned and fitted
later), `extra_instructions` up to 10,000, `ticket_number` up to 64.

## 8.2 Prompt assembly

`prompts.build_messages(ticket_text, json_reminder, extra_instructions,
kg_context, kg_low_confidence, lookups_context, playbook_context,
playbook_missing)` returns two messages:

```python
[{"role": "system", "content": SYSTEM_PROMPT},
 {"role": "user",   "content": <assembled below>}]
```

The user message is built in this order. Every block except the first is
present only when it has content.

| # | block | constant | when |
|---|---|---|---|
| 1a | task line + `--- BEGIN TICKET TEXT ---` ... `--- END TICKET TEXT ---` | `USER_PROMPT_TEMPLATE` | no instructions |
| 1b | task line **is the engineer's instruction** (`>>> ...`), the ticket labelled "background only" | `INSTRUCTED_PROMPT_TEMPLATE` | the engineer typed instructions |
| 2 | `--- CLUSTER LOOKUPS ---` | `CLUSTER_LOOKUPS_TEMPLATE` | lookups ran (chapter 6) |
| 3 | `--- RC WEBSITE SOURCES ---` with the rules for using them | `KG_CONTEXT_TEMPLATE` | sources are in the prompt (8.4) |
| 3b | "retrieval matched these sources only weakly" | `KG_LOW_CONFIDENCE_NOTE` | rc-kg flagged `low_confidence` |
| 4 | `--- RC WORKFLOW ---` | `PLAYBOOK_TEMPLATE` | a playbook matched (chapter 7) |
| 5 | `--- INSTRUCTIONS FROM THE RC ENGINEER ---` | `EXTRA_INSTRUCTIONS_TEMPLATE` | instructions |
| 6 | `Before you answer, check:` + items | `CHECKLIST_*` | always (8.3) |
| 7 | `Respond with the JSON object only.` | `RESPOND_JSON_ONLY` | always |
| 8 | "output raw JSON starting with { ..." | `JSON_ONLY_REMINDER` | only the prompt-only strategy (8.5) |

Why this order (from the comments in `prompts.py`):

- **Lookups right after the ticket**: they are facts about it.
- **Sources between the ticket and the instructions**, so the instructions
  stay last.
- **The workflow last before the instructions**: a 7B model acts on what it
  read last, and the procedure decides what the reply is.
- **Instructions appear twice** when given: as the task line (1b) and again
  at the end (5). Appending them only as a note after a long ticket did not
  work: Qwen2.5-7B, asked for a short follow-up, rewrote the previous
  resolution four times even with the note stated first and last. A small
  model follows the task line it was given, so the task line is the
  engineer's instruction.
- **"Respond with the JSON object only." is not in the user template**: it is
  put last, so it never separates the ticket from the sources and instructions.

### The checklist

The last thing before the answer, and only the items that apply. The rules
are stated earlier, but a 7B model reading 5-8k tokens acts on what it read
last; the comments record drafts that cited none of the relevant sources and
told the researcher to run a lookup that had already run.

| item | constant | when |
|---|---|---|
| follows on from the newest RC staff entries; claims no action unless a staff entry or lookup shows it; does not send the researcher to RC or the systems team | `CHECKLIST_ALWAYS` | always |
| cite a source as `[S1]` only where it says that very thing | `CHECKLIST_SOURCES` | sources in the prompt **and** not low confidence |
| use what the lookups show; do not ask for a lookup already run | `CHECKLIST_LOOKUPS` | lookups |
| steps and reply are those of the workflow stage | `CHECKLIST_PLAYBOOK` | a playbook |
| ask for what the workflow still needs: `<items>` | `CHECKLIST_MISSING` | a playbook with missing items |
| `draft_response` is the message the engineer asked for, nothing more | `CHECKLIST_INSTRUCTIONS` | instructions |

## 8.3 The system prompt

`SYSTEM_PROMPT` (in `prompts.py`) is the same for every draft. It repeats
the JSON contract even though guided decoding enforces the shape: models write
noticeably better *content* when they know what each field is for, and the
prompt is the only thing that keeps the format if the server cannot constrain
decoding. Its sections:

| section | says |
|---|---|
| role | an experienced RC support engineer at a university HPC centre (Slurm, Lmod, toolchains, GPUs, MPI, storage, transfer, accounts) |
| input | the ticket's short description and description; a catalog request's form answers (`Request form:`, one `- question: answer` per line), to be treated as the researcher's own words; the activity oldest first, each entry headed `[date time] Author, role (kind)` with role `RC staff` or `researcher`; comments are the conversation, internal work notes must not be revealed; notifications, addresses and signatures are already removed |
| voice | you write as Research Computing; RC staff entries are your own team's state of the ticket; read the newest first and continue from them |
| format | only one JSON object, no prose, no fences, exactly the five keys, each described |
| confidence | 0.8-1.0 common, well-understood, enough detail; 0.6-0.79 likely cause, details missing; below 0.6 vague, novel or needs information only the researcher or a sysadmin has. Judge the ticket, not your own fluency |
| caveats | search the ticket before claiming something is missing; never copy an example caveat; list only what is genuinely absent; assumptions and things to verify go here; an empty list is right for a clear ticket |
| consistency | missing information you needed means confidence below 0.8 |
| errors | a recognisable error names the specific cause and leads the steps with the fix |
| reply rules | greeting, sign-off exactly `Best regards,\nResearch Computing`; no invented name and no placeholder; no invented ticket number; concise; no promised timeline, escalation, exception or quota increase; no invented cluster facts; a vague ticket gets a specific request for information; do not repeat earlier replies; never send the researcher to RC/rchelp/the systems team for this problem; never say something was done unless a staff entry or lookup shows it; do not ask them to rerun what they already ran; for a cluster fault, no diagnostic commands for the researcher |
| instructions | when the engineer's block is present, it decides what the reply is for, what it says and how long it is; it overrides the guidance above; still always the JSON, the sign-off, no placeholders, no facts or commitments nobody gave |

Rules that matter are also **checked in code** after generation (chapter 10:
`assert_signoff`, `assert_no_placeholder`, `assert_no_invented_ticket_number`,
`assert_no_promises`, `assert_confidence_consistent`, ...). The prompt
explains; the code enforces.

### Prompt version

`tracing.prompt_version()` hashes the name and text of every upper-case
string constant in `prompts.py` (SHA-256, first 12 hex digits) and lists
their names. Any wording change gets a new version. It is computed once at
import (`main._PROMPT_VERSION`) and recorded on every trace, every
`generate_draft` run, and every metrics row, so outputs can be compared
across prompt revisions (`cluster/traces.py stats --by prompt_version`).

## 8.4 When sources are left out

`prompts.instruction_wants_sources(extra_instructions)` returns `False` when
the engineer asked for a follow-up or closure that needs no page:

- `_FOLLOW_UP` matches the instruction (close/closure, follow up, check in,
  any update, reminder, resolved, no response, nudge, ping, thanks,
  acknowledge, let them know, confirm it works), and
- `_NEEDS_SOURCES` does not (link, doc(s), documentation, page, how to,
  explain, step(s), instruction(s), guide, command(s), cite, source(s),
  reference(s), tutorial).

No instruction always means sources. The docstring records the measurement:
on 41 runs (2026-10-26/27), 40 were such instructions, none cited a source,
and the sources took about 2,100 tokens (25-30% of the prompt) that the
thread needed.

When it returns `False`, `main.draft` passes an empty `kg_context`. The
sources are still retrieved and kept in the trace (tag
`kg:sources_not_needed`), and the citation step still runs against them.

## 8.5 The model client and structured output

`llm_client.generate_draft(ticket_text, extra_instructions, trace,
kg_context, kg_low_confidence, lookups_context, playbook_context,
playbook_missing)` returns `(LLMDraft, model name)` or raises `LLMError`.

### The client

`get_client()` builds an `openai.AsyncOpenAI` per request (closed in
`finally`):

| setting | value | source |
|---|---|---|
| `base_url` | `VLLM_BASE_URL` (default `http://localhost:8000/v1`) | `Settings.vllm_base_url` |
| `api_key` | `VLLM_API_KEY`, else `"EMPTY"` (the SDK needs one; vLLM ignores it) | `Settings.effective_api_key` |
| `timeout` | `REQUEST_TIMEOUT_SECONDS`, default 120 | |
| `max_retries` | 1 | |
| `model` | `VLLM_MODEL_NAME` | |
| `temperature` | `TEMPERATURE`, default 0.2 (low keeps the JSON stable) | |
| `max_tokens` | `MAX_TOKENS`, default 1400 | |

**Proxy.** Explorer's nodes set `http_proxy`, and the site proxy refuses
internal traffic. `ensure_proxy_bypass()` appends the model server's host to
`no_proxy`/`NO_PROXY` (every httpx version honours it), unless
`USE_PROXY_ENV=true`.

### Strategies

Three structured-output strategies, best first (`_STRATEGIES`):

| strategy | `response_format` | effect |
|---|---|---|
| `json_schema` | `{"type": "json_schema", "json_schema": {"name": "rc_draft_response", "strict": true, "schema": DRAFT_JSON_SCHEMA}}` | guided decoding: vLLM can only emit the schema |
| `json_object` | `{"type": "json_object"}` | JSON mode: any valid JSON object (older vLLM builds) |
| `none` | not sent | prompt-only; the user message ends with `JSON_ONLY_REMINDER` |

The loop, per strategy:

1. Build the messages (`json_reminder` only for `none`) and call
   `chat.completions.create`.
2. **Server rejects the parameter** -> next strategy. `_is_unsupported_param_error`
   is true for HTTP 400/404/422/500 whose body mentions `response_format`,
   `json_schema`, `guided`, `unsupported`, `unrecognized`,
   `unexpected keyword` or `extra inputs are not permitted`. Any other API
   error is raised.
3. **`finish_reason == "length"`** -> `LLMError("The model's answer was cut
   off before it finished.", "Increase MAX_TOKENS ...")`. No fallback: a
   longer answer would be cut again.
4. **Parse**: `LLMDraft.model_validate(extract_json(raw))`. If it fails under
   a constrained strategy, the constraint is not really being applied, so try
   the next strategy. Under `none` it is an `LLMError` ("did not return valid
   JSON").
5. **Success**: remember the strategy in the module-level `_working_strategy`
   and return.

`_working_strategy` means the discovery cost is paid at most once per
process: the next draft starts at the strategy that last worked (it never
goes back up the list).

### `extract_json`

Handles what models do when they ignore the format, in order: plain
`json.loads`; the content of a fenced ```` ```json ```` block; the first
balanced `{...}`, scanning with string and escape awareness so braces inside
strings do not count. Empty output and no object are `ValueError`.

### Errors

Every failure becomes an `LLMError(message, detail)` that is safe to show in
the UI; `main.draft` records the run and returns HTTP 502 with
`{error, detail, request_id}`.

| exception | message |
|---|---|
| `APITimeoutError` | "The model server timed out (after Ns)." -- try again or raise `REQUEST_TIMEOUT_SECONDS` |
| `APIConnectionError` | "Could not reach the model server at <url>." |
| `AuthenticationError` | "The model server rejected our credentials." -- set `VLLM_API_KEY` |
| `NotFoundError` | "The model '<name>' is not served at <url>." -- check `GET /v1/models` |
| other `APIStatusError` | "The model server returned an error (HTTP n)." |
| no choices | "The model returned no completions." |

### What every attempt records

`trace["attempts"]` gets one entry per model call, including rejected
strategies and unparseable output, so a draft's cost and history can be
reconstructed from its run log:

| field | |
|---|---|
| `n`, `strategy`, `response_format`, `json_reminder`, `started_at` | what was tried |
| `outcome` | `ok`, `unsupported`, `truncated`, `parse_error` or `error` |
| `usage` | `prompt_tokens`, `completion_tokens`, `total_tokens` as vLLM reported them |
| `latency_ms`, `served_model`, `finish_reason`, `raw_response` | |
| `http_status`, `error` | on failures (truncated to 400 characters) |
| `run_id` | the `llm` run in the run tree |

`trace["params"]` holds model, temperature, max_tokens and timeout;
`trace["messages"]`, `trace["raw_response"]` and `trace["strategy"]` hold the
final call. Each call is an `llm` run (tag `strategy:<name>`, metadata
`invocation_params`, `ls_provider: vllm`, `ls_model_name`); each parse is a
`parse_draft` run (type `parser`).

### Health and context window

| function | does |
|---|---|
| `check_health()` | `GET /v1/models`; `(reachable, model ids, detail)`; never raises |
| `context_window()` | the served model's `max_model_len` from `/v1/models`, cached per model name; `None` if the server does not say |
| `token_breakdown(parts)` | tokens per prompt part with vLLM's `/tokenize` (beside `/v1`), each part on a worker thread, 3 s timeout; `None` if unavailable |

## 8.6 Token budgeting

The ticket is the only part of the prompt that is cut to fit. Before the
call, `main.draft` works out what is left for it:

```python
window   = await context_window()                     # e.g. 16384
est      = lambda s: int(len(s or "") / CHARS_PER_TOKEN)   # 3.0 chars per token
reserved = (MAX_TOKENS + est(SYSTEM_PROMPT) + est(kg_prompt_context)
            + est(lookups_context) + est(playbook_context)
            + 2 * est(extra_instructions) + 700)       # instructions twice; templates
budget   = TICKET_MAX_TOKENS if window is None else max(800, min(TICKET_MAX_TOKENS, window - reserved))
fitted   = ticket_context.fit(prepared, int(budget * CHARS_PER_TOKEN))
```

| quantity | value |
|---|---|
| `CHARS_PER_TOKEN` | 3.0 (`ticket_context.py`): English with commands and IDs runs about 3.5-4 characters per token, so 3 errs on the safe side. Budgets only; exact counts come from vLLM afterwards |
| `MAX_TOKENS` | 1400, reserved for the reply |
| `TICKET_MAX_TOKENS` | 6000, the most the ticket may take |
| floor | 800 tokens, whatever the window |
| fixed allowance | 700 tokens for templates and checklist |

`ticket_context.fit` (chapter 5) keeps the description, the first entry and
the newest entries of a long thread. The result's stats go into
`response.context` (`context_window`, `budget_tokens`, entries omitted, ...);
a trimmed ticket gets the tag `ticket_trimmed`.

### Accounting afterwards

`main._token_summary(attempts, breakdown)`:

- totals over **every** call the draft made (fallback attempts cost tokens too);
- `model_calls`, `calls_with_usage`, and `final_call` (the usage of the call
  that succeeded);
- with `TRACE_TOKEN_BREAKDOWN=true` (default), `prompt_breakdown`: tokens for
  `system_prompt`, `ticket_text`, `rc_sources`, `cluster_lookups` and
  `engineer_instructions`, counted by the model's own tokenizer, plus
  `template_and_scaffolding` = final prompt tokens minus their sum.

The totals go to `response.usage`, the root run's `usage`, the run log and
the metrics row. `needed_fallback` (feedback) is 1 when more than one call
was made.

## 8.7 Changing a prompt

1. Edit the constant in `prompts.py`. The prompt version changes by itself.
2. If the rule matters, add or update the assertion that checks it
   (`evaluators.py`, chapter 10).
3. Run the playbook and lookup tests (they assert prompt structure),
   then compare drafts before and after on real tickets with
   `cluster/traces.py stats --by prompt_version`.

> **rc-agent:** three additions.
>
> **Revision.** `build_messages` takes `revision_notes`; when the critic,
> guardrails or reviewer send a draft back, a `--- REVISION ---` block
> (`REVISION_TEMPLATE`: "A review of your previous draft found these
> problems...") is added after the engineer's instructions and before the
> checklist. `main.write_with` reserves its tokens in the budget too, and
> packs the sources to `KG_CONTEXT_MAX_CHARS` (default 7000) by rank before
> budgeting (`context_pack.py`, chapter 16).
>
> **The agents' model calls.** `llm_client.chat(messages, tools, max_tokens,
> name, json_schema)` is one chat turn for an agent loop: it sends `tools`
> with `tool_choice="auto"`, returns `{content, tool_calls, finish_reason,
> usage, model, latency_ms}`, and parses tool arguments leniently (invalid
> JSON comes back as `{"_unparsed": ...}` so the agent can be told). With a
> `json_schema` it asks for guided decoding and, if the server rejects that,
> retries without it. A server without tool calls raises `ToolsUnsupported`
> (HTTP 400/404/422/500 mentioning tools), and the caller drafts without an
> agent.
>
> **The prompt registry.** The agents' own prompts are files,
> `app/agents/prompts/*.md` (`orchestrator`, `incident`, `request`,
> `react_plan`, `react_act`, `react_reflect`, `react_user`, `reviewer`,
> `reviewer_user`), loaded by `agents/prompt_registry.py`. Each `Prompt` has a
> `version`: the first 8 hex digits of the SHA-256 of its text. A change is a
> reviewable diff and a new version; `prompt_registry.versions()` goes into
> every draft's `manifest`. `render(**values)` fills `{placeholders}` and
> raises `KeyError` if one is missing, so a renamed placeholder cannot ship an
> unfilled prompt. Files in `$RC_AGENT_PROMPTS` replace same-named ones -- how
> `evals/prompt_ab.py` A/B-tests a variant without touching the baseline.
> The writer's prompt stays in `prompts.py`, versioned by
> `tracing.prompt_version()` as above.
