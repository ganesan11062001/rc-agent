# rc-agent: TicketMate as an agent

rc-agent is TicketMate with one step added before the draft is written: the
model decides what else to look up. Everything else -- the extension, the
panel, the backend's API, the run logs, rc-kg -- is TicketMate's.

## What changes in a draft

```
TicketMate                          rc-agent
----------                          --------
clean the ticket                    clean the ticket
rules pick lookups, run them        rules pick lookups, run them
retrieve on the researcher's words  retrieve on the researcher's words
patterns pick a playbook            patterns pick a playbook
                                    AGENT: the model reads the ticket and all of the
                                    above, and calls read-only tools for anything else
                                    the reply needs (2-4 turns, at most 8 calls)
write the draft (one model call)    write the draft (the same call, from everything gathered)
citations, checks, run log          citations, checks, run log
```

The rules still run first, so the agent starts from what TicketMate would
have had. **It can only add.** If the model calls no tool, if the model
server has no tool calls, or if anything in the agent fails, the draft is
exactly TicketMate's; the run log says which (`agent:done`,
`agent:tools_unsupported`, `agent:error`).

Examples of what the rules miss and the agent can fetch:

- a node or job named only in pasted output, or in a later message;
- a search in its own words when the researcher's wording misses the page
  ("my thing dies after an hour" -> "job time limit");
- the queue and FairShare for a user whose jobs "aren't starting", when the
  ticket never says "pending";
- the playbook for a request the patterns did not recognise.

## The tools (`backend/app/tools.py`)

| tool | what it does |
|---|---|
| `lookup_user` | `id`: account exists, groups |
| `lookup_job` | `sacct` + `#SBATCH` script + expected start / time left |
| `lookup_group` | `getent group` (+ group.xtra): size, membership |
| `lookup_node` | `scontrol show node`: state, partitions, GPUs, reason |
| `lookup_partition` | `sinfo -N -p`: node states, down/drained nodes with reasons |
| `lookup_queue` | `squeue -u`: jobs, expected starts, pending reasons |
| `lookup_fairshare` | `sshare`: FairShare, group usage spread (other members never named) |
| `lookup_job_history` | `sacct -u`, 7 days: counts by state, jobs that did not complete |
| `lookup_project_owner` | `stat /projects/<name>`: owner |
| `lookup_library` | `ldconfig -p`: library present, or other versions |
| `search_rc_docs` | rc-kg `/retrieve`; new pages are added to the sources (at most 8 in all) |
| `load_playbook` | one of RC's procedures, for the writer |

**All read-only, all already in TicketMate.** Each tool wraps a function the
pipeline uses. Every lookup validates its own argument against the same
strict pattern as before and runs one allowlisted program with a fixed argv,
no shell -- so a hostile or hallucinated argument (`d4043; rm -rf`) is
refused and nothing runs (tested). Nothing that changes anything is a tool:
`aicr-access.bash`, reservations, `project ... add members`, node
drain/resume stay steps for the RC engineer.

The agent's model turns and every tool call are runs in the draft's trace
(`agent`, `agent.step`, `tool.<name>`); the run log's `trace.agent` has the
calls, whether each was refused, and the token use. The panel's context line
says `agent: N lookups/searches`.

## Running it

The agent needs a model server that does tool calls. `cluster/serve_glm.sh`
now starts Qwen2.5 with `--enable-auto-tool-choice --tool-call-parser hermes`
(GLM already had its parsers); `QWEN_TOOLS=0` turns that off. Without it the
backend logs once and drafts exactly as TicketMate.

| setting | default | |
|---|---|---|
| `AGENT_MODE` | `true` | `false` = TicketMate's pipeline, nothing else changes |
| `AGENT_MAX_STEPS` | `4` | model turns |
| `AGENT_MAX_TOOL_CALLS` | `8` | tool calls in all; a repeated call is answered from cache |

The Open OnDemand app is "RC Agent", installed as `~/ondemand/dev/rc-agent`,
so it sits next to TicketMate's "RC Copilot" without replacing it.

## Adding a tool

1. Write the function (read-only; validate its argument inside it).
2. Add a `Tool(name, description, parameters, handler)` to `tools.registry()`;
   the handler returns a short text for the model and records what it found
   in the `ToolContext` (lookups, sources or playbooks) for the writer.
3. Add a case to `tests/agent/test_agent.py`, including a hostile argument.

## Tests

| | |
|---|---|
| `tests/agent/test_agent.py` | the loop, merging, refusals, budgets, fallbacks (scripted model, fake binaries) |
| `tests/rc_kg/run_contract.sh` F, G | end to end with real rc-kg: a tool-calling stub model, and one without tool calls |

Not tested yet: a real Qwen/GLM deciding which tools to call (no GPU in the
session that built this). Try it on tickets and compare `agent:*` tags and
drafts with TicketMate's.
