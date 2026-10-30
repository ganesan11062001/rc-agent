---
name: "15. rc-agent: the agent graph"
route: /agents
menu: rc-agent
---

# 15. rc-agent: the agent graph

rc-agent keeps TicketMate's pipeline and puts a LangGraph agent graph
between gathering and the reply. This chapter covers the graph, its state,
the specialists' reasoning loop, the tools, the skills, the reviewers, the
budgets, what happens when something fails, and how every step is traced.
Guardrails and context engineering are chapter 16; traces, evals and the
release gate are chapter 17.

All the code is in `ticketmate/backend/app/agents/`:

| file | what it holds |
|---|---|
| `graph.py` | the outer graph: orchestrator, specialists, write, review; `run`, `provenance`, `manifest` |
| `react.py` | the specialists' ReAct subgraph: plan, reason, act, reflect |
| `state.py` | `GraphState` and the records it is made of: `Evidence`, `ToolCallRecord`, `Review` |
| `tools.py` | the 12 read-only tools, their argument models, scoping and `call` |
| `skills.py` | playbooks seen as skills: catalog, trigger, load |
| `critic.py` | the rule-based critic |
| `reviewer.py` | the reviewer agent |
| `prompt_registry.py`, `prompts/*.md` | the agents' prompts as versioned files |

The design rule is the one in `ticketmate/AGENT.md`: the agents start from
what the rules gathered and **can only add**. Nothing an agent does can
remove a lookup, a source or a skill the pipeline found.

## 15.1 Where the graph runs

`POST /api/draft` (`main.draft`) does TicketMate's steps 1-7 first (chapter
1): clean the ticket, run the input guardrails, start the cluster lookups,
retrieve from rc-kg, match playbooks. It then builds the seed evidence and
hands it to the graph:

```python
evidence = agent_state.Evidence(lookups=list(found_lookups), kg=kg, skills=list(found_playbooks))
if settings.agent_mode:
    final = await agent_graph.run(write_with, request_id=..., ticket=ticket, query=prepared.query,
                                  instructions=..., ticket_number=..., evidence=evidence, ...)
else:
    draft_dict, model_name = await write_with(evidence)
```

`write_with` is TicketMate's writer -- one model call, a JSON draft -- as a
closure inside `main.draft`. The graph is given it as its `writer`, so the
pipeline and the agents share one writer. After the graph returns, the draft
goes through citations, the output guardrails, evaluators and recording
exactly as in TicketMate.

`write_with` keeps what its last call used in a dict `ws` (lookups, sources,
skills, fitted ticket, prompt sections, context info). Everything after the
graph -- citations, the run log, the panel -- reads `ws`, so it sees the
evidence of the final draft, including whatever the agents added.

`AGENT_MODE=false` skips the graph and calls `write_with` once. Note that the
other rc-agent changes (input/output guardrails, the second retrieval
search, source packing) still apply in that mode; see chapter 18.

## 15.2 The graph

```
                       START
                         |
                         v
                  +--------------+
                  | orchestrator |   rules first, then the model, else "incident"
                  +--------------+
              incident |   request |    followup
             +---------+     +-----+------------------+
             v               v                        |
     +----------------+ +----------------+            |
     | incident agent | | request agent  |            |
     |  (ReAct)       | |  (ReAct)       |            |
     +----------------+ +----------------+            |
             |               |                        |
             +-------+-------+------------------------+
                     v
                 +-------+   writer(evidence, revision notes, route)
          +----->| write |   TicketMate's writer: one call, JSON draft
          |      +-------+
          |          v
          |      +--------+  critic (rules) + guardrails (output)
          |      | review |  + reviewer agent (one model call)
          |      +--------+
          |   problems and     |  no problems, or
          +--- revisions < max |  revisions == AGENT_MAX_REVISIONS
                               v
                              END

each agent node runs its own subgraph (react.py):

  START -> plan --("nothing more needed", no gaps)--> END
             |
             v
           reason --(tool calls)--> act --(steps and calls left)--> reason
             |                       |
             | (no calls)            +--(budget spent)--> reflect
             v
           reflect --(gaps, first time, budget left)--> reason
             |
             v
            END
```

The outer graph is built in `graph.build` from a `StateGraph(GraphState)`:

| edge | from -> to | decided by |
|---|---|---|
| fixed | `START` -> `orchestrator` | |
| conditional | `orchestrator` -> `incident` / `request` / `write` | `after_route`: the state's `route`; `followup` goes straight to `write` |
| fixed | `incident` -> `write`, `request` -> `write` | |
| fixed | `write` -> `review` | |
| conditional | `review` -> `write` / `END` | `after_review`: any of the last three reviews has problems **and** `revisions < max_revisions` |

`graph.run` compiles the graph per draft and calls
`graph.ainvoke(init, config={"recursion_limit": 40})`. The initial state
holds the inputs, the seed evidence and `revisions: 0`.

## 15.3 The state

`state.py` defines one `GraphState` per draft, a `TypedDict` with
`total=False`. LangGraph merges what each node returns into it. Fields
marked `Annotated[..., operator.add]` accumulate (every node appends to
`path`, every tool call to `tool_calls`); the others are replaced.

| field | type | set by | meaning |
|---|---|---|---|
| `request_id` | `str` | `run` | the draft's request id |
| `ticket` | `str` | `run` | the cleaned ticket, entries labelled RC staff / researcher |
| `query` | `str` | `run` | the researcher's own words (`ticket_context.Prepared.query`) |
| `instructions` | `str` | `run` | what the RC engineer typed |
| `ticket_number` | `str` | `run` | the ticket number from the page |
| `route` | `"incident" \| "request" \| "followup"` | orchestrator | the route |
| `route_reason` | `str` | orchestrator | why: starts with `rules:`, `model:`, or ends with `default: incident` |
| `evidence` | `Evidence` | `run`, specialists | what the reply may be written from |
| `plan` | `str` | specialist | the plan turn's text |
| `reflections` | `List[str]`, accumulates | specialist | the gaps the reflection found |
| `tool_calls` | `List[ToolCallRecord]`, accumulates | specialist | every tool call |
| `agent_stopped` | `str` | specialist | why the agent stopped (see 15.5) |
| `agent_steps` | `int` | specialist | model turns after the plan |
| `draft` | `Dict` | write | the `LLMDraft` as a dict |
| `model` | `str` | write | the model that wrote it |
| `reviews` | `List[Review]`, accumulates | review | every review of every pass |
| `revisions` | `int` | write | how many times the draft was rewritten |
| `path` | `List[str]`, accumulates | every node | the nodes run, in order |
| `usage` | `List[Dict[str, int]]`, accumulates | orchestrator, specialists | token usage of the agents' model calls |

`run` adds `latency_ms` to the final state after the graph returns.

The records are dataclasses, so the whole state can be written to the run
log -- the module docstring's point: "the draft's trace is the state's
history".

| record | fields |
|---|---|
| `Evidence` | `lookups: List[Lookup]`, `kg: Optional[KGContext]`, `skills: List[Match]`; `summary()` gives commands, sources (ref, title, url), `low_confidence` and skills for the trace |
| `ToolCallRecord` | `agent`, `step`, `tool`, `arguments`, `ok`, `result` (capped), `latency_ms`, `cached` |
| `Review` | `reviewer` (`"critic"`, `"guardrails"` or `"reviewer"`), `problems`, `unsupported_claims`, `score` (reviewer: share of claims supported, 0-1), `detail`; `ok` is `not problems` |

`Evidence` is one object shared by the whole draft: a specialist's
`ToolContext` holds the same object, and tool handlers append to its lists
or replace its `kg`.

## 15.4 The orchestrator

`graph._route` decides the route. Rules come first "because they are cheap
and certain"; the model is asked only when no rule decides.

| order | rule | route | `route_reason` |
|---|---|---|---|
| 1 | the engineer typed instructions, and either a skill with `on: instructions` matched them or `prompts.instruction_wants_sources` says they need no page | `followup` | `rules: the engineer asked for a follow-up or closing message` |
| 2 | a skill with `on: researcher` triggered | that skill's `route:` header (first one) | `rules: skill <name> triggered (a <route>)` |
| 3 | the ticket number, or a `Ticket:` line in the ticket, starts with `TASK`, `RITM` or `REQ` followed by a digit | `request` | `rules: a ServiceNow request (TASK/RITM)` |
| 4 | otherwise, the model | `incident` or `request` | `model: <its reason>` |
| 5 | the model fails or answers something else | `incident` | `... default: incident` |

The model call (`llm_client.chat`) uses the `orchestrator` prompt as the
system message and the researcher's words (`query`, else the ticket; at most
3,000 characters) as the user message, `max_tokens=80`, and guided JSON with
`ROUTE_SCHEMA`: `{"route": "incident" | "request", "reason": string}`, no
other keys. If the server has no guided decoding, `chat` asks again without
it and the answer is parsed leniently (`extract_json`). The prompt
(`prompts/orchestrator.md`) defines each route and gives seven few-shot
examples.

On the ground truth the rules decide 63.6% of the routed cases
(`routing.rule_routed_share` = 0.636 in the 2026-10-29 routing report); the
model decides the rest.

A `followup` draft runs no specialist: the engineer asked for a follow-up or
closing message, so there is nothing to investigate.

## 15.5 The specialists and the ReAct loop

`graph._specialist(agent, ...)` builds the `incident` and `request` nodes.
Each node:

1. makes a `ToolContext` (the ticket, the shared evidence, the request id,
   the lookup timeout, an empty call cache);
2. renders the `react_user` prompt: the ticket (first 6,000 characters), the
   engineer's instructions in `--- INSTRUCTIONS FROM THE RC ENGINEER ---`
   markers if any, and `--- ALREADY GATHERED ---` -- the evidence so far as
   `react.gathered_text` writes it (each lookup's command and summary; each
   source's ref, page title and heading, marked `(weak match)` when retrieval
   was low-confidence; each loaded skill and what it is missing);
3. runs `react.run` with the agent's system prompt (`prompts/incident.md` or
   `prompts/request.md`);
4. returns the evidence, plan, tool calls, reflections, stop reason, steps,
   usage, and a path such as `["incident_agent", "incident.plan",
   "incident.reason", "incident.act", ...]`.

The two system prompts differ in what they ask for: the incident agent
diagnoses (facts first, then the knowledge graph, the researcher's fault vs
the cluster's); the request agent finds the procedure (load the skill,
check the account, groups and project owner). Both say they cannot change
anything, must never repeat a lookup already gathered, must use only names
and IDs that appear in the ticket, and must stop as soon as they have what
the reply needs. `tests/agent/test_prompts.py` checks that these rules are
stated.

### The subgraph

`react.build(agent, max_steps, max_tool_calls)` compiles a
`StateGraph(ReactState)`:

| `ReactState` field | meaning |
|---|---|
| `agent`, `query`, `ctx` | which agent; the researcher's words; the `ToolContext` |
| `messages` | the chat so far (system, user, plan, assistant tool calls, tool results) |
| `plan` | the plan text |
| `steps`, `calls` | model turns after the plan; tool calls that ran (cached ones do not count) |
| `pending` | the tool calls from the last turn, not run yet |
| `reflected` | the reflection has already been put to the model |
| `stopped`, `notes` | the stop reason, and the model's last text or the error |
| `records`, `reflections`, `usage`, `path` | accumulate |

| node | what it does |
|---|---|
| `plan` | one model call **without tools** (`max_tokens=200`), the `react_plan` prompt appended: "In at most five short numbered lines, write what the reply needs that is not gathered yet, and which tool gets each". The docstring's reason: it "keeps a 7B model from calling tools at random". If the plan says "nothing more needed" **and** the rules find no gaps, the agent stops (`done`). Otherwise the `react_act` prompt (the plan, then "call the tools you need ... answer with one short line starting `done:`") is appended |
| `reason` | one model call with this agent's tool schemas, `tool_choice="auto"` (default `max_tokens=600`). Calls beyond the remaining call budget are dropped; if every requested call was dropped, `stopped = "call budget"` |
| `act` | runs every pending call concurrently (`asyncio.gather` over `tools.call`) and appends one `tool` message per call. `calls` grows by the calls that were not served from cache |
| `reflect` | a deterministic check, `react.gaps` (below). If there are gaps, the reflection has not run yet, and steps and calls are left, the `react_reflect` prompt lists them ("Do these now, or answer `done:` with the reason they are not needed") and the loop returns to `reason`. Otherwise the agent stops |

| routing function | rule |
|---|---|
| `after_plan` | `END` if `stopped` is set, else `reason` |
| `after_reason` | `END` on `tools unsupported` or `error`; `act` if calls are pending; else `reflect` |
| `after_act` | `reflect` when `calls >= max_tool_calls` or `steps >= max_steps`; else `reason` |
| `after_reflect` | `END` if `stopped` is set, else `reason` |

`react.gaps` -- what is "obviously still missing", only for tools this agent
has:

| gap | condition |
|---|---|
| a named job, node, user or partition not looked up | `lookups.find_candidates(query)` names it, the matching `lookup_*` tool is in scope, and no lookup of that kind and target is in the evidence |
| no skill for a request | request agent, no skill in the evidence, `load_skill` in scope |
| no documentation for an incident | incident agent, sources missing, not `ok`, empty or low-confidence, and `search_knowledge_graph` not called yet |

The reflection runs once per agent. It reads the researcher's words
(`query`), not RC's replies.

### Stop reasons

`agent_stopped` (the run log's `trace.agent.stopped`, the root tag
`agent:<reason>`, the metrics row's `agent_stopped`) takes these values:

| value | when |
|---|---|
| `done` | the plan said nothing more was needed, or the reflection found nothing (or had already run, or the budget was spent) |
| `call budget` | the model asked for tools after the call budget was used up |
| `tools unsupported` | the server refused a request with `tools` (`llm_client.ToolsUnsupported`) |
| `error` | the plan or a reasoning call failed, or the subgraph raised |
| `skipped` (tag only) | the route was `followup`: no specialist ran |

`react.run` wraps the whole subgraph in `try/except`: an exception becomes
`stopped = "error"` with the evidence gathered so far. It never raises.

## 15.6 Tools

Every tool in `tools.py` is

- a **pydantic argument model** -- the JSON schema the model is given is
  generated from it, and the model's arguments are validated against it
  before anything runs;
- a **handler** that wraps a function TicketMate's pipeline already uses;
- the **agents** allowed to use it.

All are read-only. The module docstring: "Nothing that changes anything is
a tool, or can become one: RC's scripts, reservations, `project ... add
members` stay steps for the RC engineer." `tests/agent/test_agent.py` checks
that no tool name contains `add`, `create`, `delete`, `update`, `reserve`,
`exec`, `push`, and similar.

| tool | arguments (model) | wraps | runs | incident | request |
|---|---|---|---|---|---|
| `lookup_user` | `username` (`UserArgs`) | `lookups._user` | `id <user>` | yes | yes |
| `lookup_job` | `job_id` (`JobArgs`) | `lookups._job`, then `_job_script` and `_job_queue` if found | `sacct -j`, `sacct -j --batch-script`, `squeue -j --start` / `-O TimeLeft` | yes | |
| `lookup_group` | `group` (`GroupArgs`) | `lookups._group`, with the users already looked up | `getent group` | yes | yes |
| `lookup_node` | `node` (`NodeArgs`) | `lookups._node` | `scontrol show node` | yes | |
| `lookup_partition` | `partition` (`PartitionArgs`) | `lookups._partition` | `sinfo -N -p` | yes | yes |
| `lookup_queue` | `username` (`UserArgs`) | `lookups._user_queue` | `squeue -u` | yes | |
| `lookup_fairshare` | `username` (`UserArgs`) | `lookups._fairshare` | `sshare -a -P -u`, then `-A <account>` | yes | |
| `lookup_job_history` | `username` (`UserArgs`) | `lookups._history` | `sacct -u -S now-7days` | yes | |
| `lookup_project_owner` | `project` (`ProjectArgs`) | `lookups._project_owner` | `stat -c %U\|%G` on `/projects/<name>` | yes | yes |
| `lookup_library` | `library` (`LibraryArgs`) | `lookups._libraries` | `ldconfig -p` | yes | |
| `search_knowledge_graph` | `query`, 3-200 chars (`SearchArgs`) | `kg_client.retrieve`, merged with `kg_client.merge` | rc-kg `/retrieve` | yes | yes |
| `load_skill` | `name`, a `Literal` of procedure skill names (`SkillArgs`) | `skills.load` | nothing | yes | yes |

### Schemas

Each argument model sets `extra="forbid"`, so the schema has
`additionalProperties: false`. String fields take their `pattern` from the
lookups module's own regex, anchored by `_full` (`^(?:...)$`), because
"pydantic's `pattern` is a search, the lookups' check a full match". The
generated entry for `lookup_node`:

```json
{"type": "function", "function": {
  "name": "lookup_node",
  "description": "A compute node's state, partitions, GPUs and Slurm's reason if it is down or drained (scontrol show node).",
  "parameters": {"additionalProperties": false, "type": "object", "required": ["node"],
    "properties": {"node": {"type": "string", "pattern": "^(?:[a-z]{1,2}[0-9]{4})$",
                            "description": "A compute node, e.g. d4043 or c0602"}}}}}
```

`load_skill`'s argument model is built in `registry()` with `create_model`:
the name is a `Literal` of the procedure skills, so the schema lists them
and validation refuses any other name. Its description lists each skill as
`name = title`.

### A call

`tools.call(agent, step, name, raw_args, ctx, tools)` never raises. In order:

| check | result text | `ok` |
|---|---|---|
| the same tool and arguments ran before in this draft (`ctx.cache`) | `(already done) <cached text>`; `cached=True`, not counted against the budget | yes |
| the tool is not in this agent's set | `error: no tool '<name>' for the <agent> agent` | no |
| the arguments were not valid JSON (`_unparsed`, from `llm_client.chat`) | `error: the arguments were not valid JSON` | no |
| pydantic validation fails | `refused: invalid arguments -- <field>: <message>` | no |
| the handler raises | `error: <name> failed` | no |
| otherwise | the handler's text | unless it starts with `error:` or `refused:` |

Validation is the first line of defence; the lookup is the second. rc-agent
added `lookups._invalid` to every lookup function: each checks its own
argument against its pattern with `fullmatch`, so "a caller that skipped
find_candidates (an agent's tool call) still cannot pass anything else to a
command". A refused lookup has the command `(not run)`. The contract test
(stage F, chapter 17) sends `{"job_id": "1; touch /tmp/stub_pwned"}` and
checks it is refused and nothing ran.

What a handler returns is short; what it found goes into the evidence:

- **lookups** (`_keep`): lookups that ran are added to `evidence.lookups`
  unless one of the same kind and target is there; the model reads them as
  `lookups.prompt_text`, capped at `MAX_RESULT_CHARS` (1,500); a refusal
  returns the refusal's summary.
- **search** (`_search`): the new result is merged into `evidence.kg` with
  `kg_client.merge` (new pages renumbered after the existing `[S#]`, at most
  `MAX_SOURCES` = 8). The model reads only the new pages (ref, title,
  heading, the first 160 characters) or `no new pages: what it found is
  already in your sources`, plus a weak-match note when rc-kg flagged low
  confidence. The comment on `MAX_SOURCES`: "rules give 6; agents add at
  most 2 (each source is up to 2.5k chars)".
- **skills** (`_skill`): the skill's `Match` is added to `evidence.skills`;
  the model reads `loaded skill <title> (the writer gets its full
  procedure)` and what is missing.

## 15.7 Skills

A skill is a playbook file (chapter 7), seen by the agents through
`skills.py`. RC's own playbooks are private and live outside the repository
(`PLAYBOOK_DIR`, set by `cluster/env.sh`); the repository ships one example,
`app/playbooks/example-procedure.md`.

| kind | header | triggered by |
|---|---|---|
| `procedure` | `on: researcher` (default) | `match`/`unless` patterns on the researcher's words |
| `reply` | `on: instructions` | the same patterns on the engineer's instructions (follow-up and closing skeletons) |

rc-agent reads one more header line, `route: incident | request` (default
`request`): the agent a triggered skill sends the ticket to. TicketMate's
parser reads and keeps it unused. Any other value is a parse error, and a
file that does not parse is skipped with an error in the log.

```
---
name: example-procedure
title: Example -- access to a restricted software group
match: (?i)\bexample-software\b
match: (?i)\b(access|add(ed)?|join|group)\b
unless: (?i)\binstall(ation)?\b
route: request
field.username: (?<![\w@./-])([a-z][a-z0-9-]*\.[a-z][a-z0-9-]*)(?![\w@-]|\.[a-z0-9])
require.username: the cluster username of each person to add
---
...the runbook...
```

Skills reach a draft in two ways, and the trace says which:

| way | when | code |
|---|---|---|
| trigger | the rules matched before any agent ran (at most `MAX_PLAYBOOKS` = 2 procedures, plus 1 reply skill) | `playbooks.find`, called in `main.draft`; `skills.trigger` is the same for the evals |
| load | an agent called `load_skill` | `skills.load(name, ticket)`: the playbook with the values found in the ticket |

Either way the writer gets the full runbook, the values found and what is
still missing. `skills.catalog()` gives each skill's card (`SkillInfo`:
name, title, kind, route, triggers, requires) for the trace manifest and the
tool description; `skills.procedures()` is the procedure subset `load_skill`
offers.

## 15.8 Write

The `write` node calls the writer with the evidence, revision notes and the
route. On the first pass there are no notes. On a revision the notes are
the problems of the last three failing reviews, one `- ` line each. The
writer appends them to its user prompt in a `--- REVISION ---` block
(chapter 16). The node returns the draft, the model, `revisions + 1` when
there were notes, and `write` or `revise` on the path.

A writer error (`LLMError`) is not caught: it propagates out of the graph,
and `main.draft` returns 502 as in TicketMate.

## 15.9 Review

The `review` node produces two or three `Review`s per pass:

| reviewer | how | what it finds |
|---|---|---|
| `critic` | `critic.review`, deterministic | the mistakes RC's reviewers found (table below) |
| `guardrails` | `guardrails.check_output` on the draft, with the skills and lookups as evidence; only `revise` findings become problems | destructive commands, internal work-note text repeated (chapter 16) |
| `reviewer` | `reviewer.review`, one model call; skipped when `AGENT_REVIEWER=false` or the route is `followup` | claims the evidence does not support |

### The critic

Deterministic on purpose. The docstring: "The in-loop LLM judge (the same
7B model grading itself) gave correctness 1.0 to drafts the team marked as
failures; these checks are the failures they named, written as rules."

| check | rule |
|---|---|
| claims an action nobody took | `we/I have (now/already) added, granted, created, ...` or `has/have been added, ...` in the reply, and the verb's stem is not in any RC staff entry or the lookups. Reported once |
| sends the researcher to RC | `contact / reach out to / email / write to / get in touch with` + `research computing`, `rc`, `rchelp`, `systems team`, `sysadmins`, `hpc team`. Not checked on a `followup` |
| repeats RC's last message | reply over 200 characters, and more than half of its 5-word shingles are in the last RC staff entry |
| does not ask for what is missing | a skill lists missing information and the reply has no `?` and no `please send/share/provide/confirm/let us know` or `could you` |
| placeholder or wrong sign-off | `[Your Name]`, `[name]`, `[insert ...]`, `[username]`, `[job id]`, `<your name>`, or the reply does not end `Best regards,\nResearch Computing` |

Each problem is written as an instruction to the writer ("Say what will
happen instead, and put the action in the steps.").

### The reviewer agent

`reviewer.review(draft, ticket, evidence)` is a separate model call with its
own prompt (`prompts/reviewer.md`), "so the writer's reasoning does not leak
into the verdict". It is shown exactly the evidence the writer had, clipped:

| part | limit |
|---|---|
| ticket | 5,000 characters |
| lookups (`lookups.prompt_text`) | 2,500 |
| sources (`evidence.kg.context`) | `MAX_EVIDENCE_CHARS` = 6,000 |
| skills | titles only |
| the draft | summary, steps, reply |

It is asked to sort every factual claim into supported, general knowledge,
or unsupported, and to return guided JSON (`REVIEW_SCHEMA`):
`unsupported_claims` (strings), `problems` (strings), `supported_share`
(0-1), `max_tokens=500`. The code keeps at most 8 claims and 6 problems
(300 characters each) and ignores a `supported_share` outside 0-1. Claims
become one note ("These claims are not supported by the ticket, the lookups
or the sources -- remove them, or say they need to be checked: ...").
`supported_share` is the review's `score`: the metrics row's
`reviewer_supported_share` and the ops report's "reviewer groundedness".

If the call fails or the output does not parse, the verdict is `ok` with
`detail = "not reviewed: ..."`: review must never block a draft.

### Revision

`after_review` takes the last three reviews; if any has problems and
`revisions < AGENT_MAX_REVISIONS` (default 1), the draft goes back to
`write`. So a draft is written at most twice by default. Whatever the second
review finds is not sent back again; output guardrail findings that remain
become caveats (chapter 16).

## 15.10 Budgets and limits

| limit | value | where |
|---|---|---|
| `AGENT_MAX_STEPS` | 4 | model turns per specialist after its plan (`reason` calls) |
| `AGENT_MAX_TOOL_CALLS` | 8 | tool calls in all, per specialist; a cached repeat does not count |
| `AGENT_MAX_REVISIONS` | 1 | times review may send a draft back |
| `AGENT_REVIEWER` | `true` | the reviewer agent (one model call per review pass) |
| reflections | 1 | per specialist |
| tool timeout | `CLUSTER_LOOKUP_TIMEOUT_SECONDS` (5 s) | passed as `ToolContext.timeout` to each lookup |
| outer recursion limit | 40 | `graph.run` |
| subgraph recursion limit | `4 * max_steps + 10` | `react.run` |
| orchestrator input / output | 3,000 chars / 80 tokens | `graph._route` |
| plan output | 200 tokens, kept to 800 chars | `react.plan` |
| reasoning output | 600 tokens (`chat` default) | `react.reason` |
| ticket shown to a specialist | 6,000 chars | `graph._specialist` |
| a tool result | 1,500 chars (`MAX_RESULT_CHARS`) | `tools._clip`, `ToolCallRecord.result` |
| sources | 8 (`MAX_SOURCES`) | `tools._search` |
| reviewer output | 500 tokens; 8 claims, 6 problems | `reviewer.review` |

### Model calls per draft

Counted from the code (writer fallback strategies and the background judge,
which TicketMate also has, not included):

| | calls |
|---|---|
| orchestrator | 0 (a rule decided) or 1 |
| specialist | 0 (`followup`); else 1 plan + 0 to 4 reasoning turns |
| writer | 1, or 2 with a revision |
| reviewer | 0 (`followup` or off); else 1 per pass |
| **total** | **1** (follow-up) to **10** (model-routed, full budget, revised) |

TicketMate makes one. No rc-agent draft on a GPU has been recorded yet, so
the latency this costs has not been measured; chapter 18 has what is known.

## 15.11 Degrade, never fail

Every agent step can fail without failing the draft. Only a writer failure
does, as in TicketMate.

| failure | what happens | where it shows |
|---|---|---|
| the server refuses `tools` (vLLM without `--enable-auto-tool-choice --tool-call-parser`) | `chat` raises `ToolsUnsupported` (HTTP 400/404/422/500 mentioning tools); the specialist stops; the writer drafts from the rules' evidence | `stopped: tools unsupported`; contract stage G |
| the server has no guided decoding | `chat` retries without `response_format`; orchestrator and reviewer parse leniently | the `llm` run |
| the orchestrator's model call fails or answers nonsense | route `incident` | `route_reason` ends `default: incident` |
| plan or reasoning call fails; the subgraph raises | the specialist stops with what it gathered | `stopped: error`, the `agent.<name>` run in error |
| a tool's arguments are invalid, unknown, out of scope | refused; nothing runs; the model reads the refusal | `ToolCallRecord.ok = false`; metrics `tool_refusals` |
| a tool raises | `error: <name> failed` | the `tool.<name>` run in error |
| rc-kg is down during a search | `knowledge graph unavailable: ...` | the tool result |
| the reviewer fails | the draft passes un-reviewed | `detail: not reviewed: ...` |
| the writer fails | 502, as TicketMate | the run log's `error` |

Qwen2.5 writes tool calls in the Hermes format. `cluster/serve_glm.sh`
starts Qwen models with `--enable-auto-tool-choice --tool-call-parser
hermes` unless `QWEN_TOOLS=0` or the GLM parsers are on (GLM models get
`--tool-call-parser glm47`). Without them, the comment says, "the server
answers a request with `tools` by refusing it, and the backend drafts
without the agent".

## 15.12 The agents' prompts

The prompts are Markdown files in `app/agents/prompts/`, loaded by
`prompt_registry.py` (versions and A/B overrides: chapter 17). The writer's
prompt stays in `app/prompts.py`, as in TicketMate.

| file | role | placeholders |
|---|---|---|
| `orchestrator.md` | system: route definitions, 7 examples, the JSON to return | |
| `incident.md` | system: the incident agent | |
| `request.md` | system: the request agent | |
| `react_user.md` | user: ticket, instructions, already gathered | `{ticket}`, `{instructions}`, `{gathered}` |
| `react_plan.md` | user: the plan turn | |
| `react_act.md` | user: carry out the plan | `{plan}` |
| `react_reflect.md` | user: the gaps | `{gaps}` |
| `reviewer.md` | system: the reviewer | |
| `reviewer_user.md` | user: ticket, evidence, draft | `{ticket}`, `{lookups}`, `{sources}`, `{skills}`, `{summary}`, `{steps}`, `{reply}` |

`Prompt.render` raises if a placeholder has no value, "so a renamed
placeholder cannot silently ship an unfilled prompt".

## 15.13 How a node's work is traced

`tracing.run(name, run_type, ...)` opens a child run of whatever run is
current (a context variable; asyncio tasks inherit it), so each node opens
its runs without the trace being passed down. A draft's run tree in agent
mode:

```
draft                                   chain   root (main.draft)
├── ticket_context.prepare              tool
├── guardrails.input                    tool
├── cluster.lookups                     tool
├── rc_kg.retrieve (+ rc-kg's stages)   retriever
├── rc_kg.retrieve_expansion            retriever   when the query has an expansion
├── playbooks.match                     tool
├── graph                               chain   metadata: manifest, budgets
│   ├── graph.orchestrator              chain   outputs: route, reason
│   │   └── agent.orchestrator          llm     when the rules did not decide
│   ├── agent.incident                  chain   inputs: gathered; metadata: tools, budgets
│   │   ├── react.incident.plan         chain
│   │   │   └── agent.incident.plan     llm
│   │   ├── agent.incident.step         llm     one per reasoning turn
│   │   ├── tool.lookup_job             tool    arguments, result, ok, cached
│   │   ├── tool.search_knowledge_graph tool
│   │   └── react.incident.reflect      chain   gaps, acted_on
│   ├── graph.write                     chain   revision, notes
│   │   └── generate_draft              chain
│   │       ├── llm                     llm
│   │       └── parse_draft             parser
│   ├── graph.review                    chain   every review
│   │   └── review.reviewer             chain
│   │       └── agent.reviewer          llm
│   └── graph.write / graph.review ...          on a revision
├── tokenize.prompt_breakdown           tool
├── rc_kg.cite                          tool
├── guardrails.output                   tool
└── evaluate                            chain
```

Each run has inputs, outputs, timing, status and, for `llm` runs, token
usage and the model. Contract stage F checks that `graph`,
`graph.orchestrator`, `agent.incident`, `react.incident.plan`,
`tool.search_knowledge_graph`, `graph.write`, `graph.review`,
`review.reviewer` and `generate_draft` are all present.

Besides the run tree, `graph.provenance(final)` becomes the run log's
`trace.agent`: route and reason, plan, stop reason, steps, every tool call,
reflections, every review, revisions, path, the evidence summary, summed
token usage of the agents' calls, latency and the manifest. The root run is
tagged `route:<route>`, `agent:<stopped>`, `agent_calls:<n>` and `revised`;
`response.context.agent` (route, stopped, tool names, revisions,
`reviews_ok`) goes to the panel, which shows "agent: N lookups/searches".

## 15.14 Adding things

From `ticketmate/AGENT.md`:

| to add | do |
|---|---|
| a tool | a read-only function that validates its own argument; an argument model; a `Tool(...)` in `tools.registry()` with the agents allowed; a case in `tests/agent/test_agent.py` with a hostile argument |
| a skill | a playbook file (format in `app/playbooks.py`, including `route:` and `require.*`); a routing case in `tests/playbooks/test_playbooks.py`; a ground-truth case in `evals/groundtruth.yaml` |
| a prompt change | a variant directory, `evals/prompt_ab.py`, then copy it in (chapter 17) |
