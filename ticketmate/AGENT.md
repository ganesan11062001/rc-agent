# rc-agent: TicketMate as a multi-agent system

rc-agent keeps everything TicketMate has -- the extension, the panel, the
backend API, rc-kg, the cleaning, lookups, skills and the writer -- and puts
a LangGraph agent graph between gathering and the reply. Operations
(traces, evaluation, guardrails, optimization) are in [LLMOPS.md](LLMOPS.md).

## The graph (`backend/app/agents/`)

```
START -> orchestrator --+-- incident agent --+
                        +-- request agent ---+--> write --> review --+--> END
                        +-- (follow-up) -----+       ^               |
                                                     +--(problems)---+   at most AGENT_MAX_REVISIONS (1)

each agent (react.py), a ReAct subgraph:
  plan --> reason <--> act (tool calls -> observations) --> reflect --(gaps, once)--> reason --> END
```

| node | what it does | file |
|---|---|---|
| gather (before the graph) | TicketMate's rules: clean the ticket, run the lookups it names, retrieve on the researcher's words, trigger skills | `main.py` |
| orchestrator | routes: rules first (follow-up instruction -> followup; a triggered skill -> its `route:`; TASK/RITM -> request), else the model (guided JSON, few-shot), else incident | `graph.py`, `prompts/orchestrator.md` |
| incident agent | diagnoses: jobs, nodes, partitions, queue, FairShare, recent failures, libraries, the knowledge graph | `prompts/incident.md` |
| request agent | procedures: skills, accounts, groups, project owners, the knowledge graph | `prompts/request.md` |
| plan / reason / act / reflect | ReAct with an explicit plan turn (no tools), concurrent tool calls, and a deterministic reflection (a job/node/user the researcher named and nobody looked up; no skill for a request; no doc search for an incident) | `react.py`, `prompts/react_*.md` |
| write | TicketMate's writer: one call, JSON draft, from the evidence; revision notes on the second pass | `main.py` (`write_with`) |
| review | critic (rules from the team's reviews), guardrails (destructive commands, leaked work notes), reviewer agent (every claim checked against the evidence) | `critic.py`, `../guardrails.py`, `reviewer.py` |

The agents start from what the rules gathered and **can only add**: no
tool call, a server without tool calls, or any failure leaves TicketMate's
draft. `AGENT_MODE=false` is exactly TicketMate.

## State, tools, skills, prompts

- **State** (`state.py`): one typed `GraphState` per draft -- inputs, route
  and reason, `Evidence` (lookups, knowledge-graph sources, skills), plan,
  every `ToolCallRecord`, reflections, every `Review`, revisions, the path
  through the graph, token usage. The run log's `trace.agent` is this state.
- **Tools** (`tools.py`): 12, all read-only. Each has a pydantic argument
  model; the function-calling schema is generated from it (anchored
  patterns, closed objects, a `Literal` of skill names), and arguments are
  validated before the handler runs -- then again by the lookup itself.
  Scoped per agent (the request agent has no job/node tools). Nothing that
  changes anything is a tool.
- **Skills** (`skills.py` over `app/playbooks/*.md`): RC's procedures with
  their triggers (`match`/`unless`, read from the researcher's words or the
  engineer's instructions), their `route`, what they `require`. Reach a draft
  by trigger or by an agent's `load_skill`; the trace says which.
- **Prompts** (`prompt_registry.py`, `prompts/*.md`): one file each, a
  version hash each, recorded in every trace's manifest with the skills' and
  tools'. `RC_AGENT_PROMPTS=<dir>` overrides same-named files (A/B tests).

## Settings

| setting | default | |
|---|---|---|
| `AGENT_MODE` | `true` | `false`: TicketMate's pipeline |
| `AGENT_MAX_STEPS` | `4` | model turns per agent after its plan |
| `AGENT_MAX_TOOL_CALLS` | `8` | in all; a repeat is served from cache |
| `AGENT_MAX_REVISIONS` | `1` | times review may send a draft back |
| `AGENT_REVIEWER` | `true` | the reviewer agent (one model call) |
| `KG_CONTEXT_MAX_CHARS` | `7000` | sources fitted by rank for the writer |

The model server needs tool calls: `cluster/serve_glm.sh` starts Qwen2.5
with `--enable-auto-tool-choice --tool-call-parser hermes` (`QWEN_TOOLS=0`
off). The OOD app is "RC Agent" at `~/ondemand/dev/rc-agent`, beside
TicketMate's "RC Copilot".

## Adding things

- a **tool**: a read-only function that validates its argument; an argument
  model; a `Tool(...)` in `tools.registry()` with the agents allowed; a case in
  `tests/agent/test_agent.py` with a hostile argument.
- a **skill**: a file in `app/playbooks/` (format in `app/playbooks.py`, incl.
  `route:` and `require.*`); a routing case in `tests/playbooks/test_playbooks.py`
  and a ground-truth case in `evals/groundtruth.yaml`.
- a **prompt change**: a variant directory, `evals/prompt_ab.py`, then copy it in.
