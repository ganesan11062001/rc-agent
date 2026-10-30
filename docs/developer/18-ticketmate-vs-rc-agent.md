---
name: "18. TicketMate vs rc-agent"
route: /ticketmate-vs-rc-agent
menu: rc-agent
---

# 18. TicketMate vs rc-agent

rc-agent is a copy of TicketMate. This chapter lists every difference
between the two trees, what they share, what the agent costs, and which to
use when. The differences come from `diff -rq` of
`/projects/rc/projects/ticketmate` against `/projects/rc/projects/rc-agent`
(git-ignored and generated files left out).

## 18.1 In one paragraph

TicketMate gathers with rules and drafts with **one** model call. rc-agent
gathers with the same rules, then lets an orchestrator and two ReAct agents
add lookups, searches and skills with read-only tools, drafts with the same
writer, and has the draft reviewed (critic, guardrails, reviewer agent) and
rewritten once if needed. It also adds input and output guardrails, a
second retrieval search in documentation vocabulary, rank-based packing of
sources, a ground-truth evaluation harness, a prompt registry and a release
gate. The knowledge graph, the extension, the OOD app template and the
trace store are shared or the same.

## 18.2 What is only in rc-agent

| path | what | chapter |
|---|---|---|
| `ticketmate/backend/app/agents/` | the agent graph: `graph.py`, `react.py`, `state.py`, `tools.py`, `skills.py`, `critic.py`, `reviewer.py`, `prompt_registry.py`, `prompts/*.md` (9 files) | 15 |
| `ticketmate/backend/app/guardrails.py` | input and output guardrails | 16 |
| `ticketmate/backend/app/context_pack.py` | sources fitted to a budget by rank | 16 |
| `ticketmate/evals/` | `groundtruth.yaml` (22 cases), `run.py` (suites and gates), `prompt_ab.py` | 17 |
| `ticketmate/tests/agent/` | `test_agent.py`, `test_guardrails.py`, `test_otel.py`, `test_prompts.py`, `replay_eval.py` | 17 |
| `ticketmate/tests/run_all.sh` | the 13-stage release gate | 17 |
| `ticketmate/AGENT.md`, `ticketmate/LLMOPS.md` | the agent and LLMOps summaries | |
| `docs/developer/` | this guide | |

Only in TicketMate: `rc-copilot/`, the published OOD release (a git-ignored
deploy target written by `ood-apps/publish.sh`). rc-agent has not been
published: there is no `/projects/rc/projects/rc-agent/rc-copilot/`.

## 18.3 Files that differ

| file | TicketMate | rc-agent |
|---|---|---|
| `backend/app/main.py` | clean -> lookups + `retrieve` -> playbooks -> fit -> `generate_draft` once -> citations -> evaluators -> record | adds `guardrails.input` after cleaning; `retrieve_multi` instead of `retrieve`; the writer becomes the closure `write_with(evidence, revision_notes, route)` that also packs sources and fits the ticket per call; runs the agent graph when `AGENT_MODE`; `guardrails.output` after citations, with caveats and `traces/$USER/guardrails/`; root metadata `project: rc-agent/...` and a `config` snapshot; root tags `route:`, `agent:`, `agent_calls:`, `revised`, `guardrail:`; run log `trace.agent` and `trace.guardrails`; metrics row with `mode`, route, tools, reviews, guardrails, manifest |
| `backend/app/config.py` | | `agent_mode` (true), `agent_max_steps` (4), `agent_max_tool_calls` (8), `agent_max_revisions` (1), `agent_reviewer` (true), `kg_context_max_chars` (7000) |
| `backend/app/llm_client.py` | `generate_draft` | adds `chat()` (one turn, optional tools and guided JSON, traced as an `llm` run), `ToolsUnsupported`, `_tools_unsupported`; `generate_draft(..., revision_notes=)` |
| `backend/app/kg_client.py` | `retrieve` | adds `merge` (renumbered, de-duplicated, capped) and `retrieve_multi` (the second search) |
| `backend/app/ticket_context.py` | `Prepared(text, raw_chars, stats, query)` | adds `EXPANSIONS`, `expand()`, `Prepared.expansion` |
| `backend/app/lookups.py` | arguments checked by `find_candidates` | adds `_invalid`: every lookup function checks its own argument (`fullmatch`) and refuses with `(not run)` |
| `backend/app/prompts.py` | | adds `REVISION_TEMPLATE` and `build_messages(..., revision_notes=)`; also `AGENT_SYSTEM_PROMPT` and `AGENT_USER_TEMPLATE`, which nothing uses |
| `backend/app/playbooks.py` | `route:` "read and kept, unused here" | `route:` "the agent a triggered skill sends the ticket to" (comment only) |
| `backend/app/runlog.py` | | comments only |
| `backend/requirements.txt` | | adds `langgraph>=1.0` and `pyyaml>=6` |
| `backend/web/app.js` | | shows `agent: <tool names>` in the meta line |
| `extension/content.js` | | the panel's context line adds `agent: N lookups/searches` |
| `dist/rc-copilot-extension.zip` | | built from the above |
| `cluster/serve_glm.sh` | | Qwen models get `--enable-auto-tool-choice --tool-call-parser hermes` (`QWEN_TOOLS=0` off) |
| `ood-apps/rc-copilot/manifest.yml` | `name: RC Copilot` | `name: RC Agent` |
| `ood-apps/install.sh` | links `~/ondemand/dev/rc-copilot` | links `~/ondemand/dev/rc-agent`, so TicketMate's app is untouched |
| `ood-apps/publish.sh` | | comment only |
| `tests/rc_kg/run_contract.sh` | stages A-E; expects `project: rc-copilot/full` | adds F (the agent) and G (no tool calls); expects `rc-agent/full` |
| `tests/rc_kg/stub_vllm.py` | | answers the orchestrator, reviewer, plan and tool turns; `STUB_NO_TOOLS=1` refuses tools |
| `tests/ticket_context/test_ticket_context.py` | | adds the query-expansion checks |
| `tests/README.md` | | a row for `agent/test_agent.py` |
| `README.md` | | a header saying this is rc-agent; paths |
| `ood-apps/MEMBERS.md` | paths under `ticketmate` | paths under `rc-agent` |
| `.gitignore` | | `/.cache` and `/.uv` listed twice |

### Behaviour, side by side

| | TicketMate | rc-agent (`AGENT_MODE=true`) |
|---|---|---|
| who decides what to look up | rules (`lookups.find_candidates`) | rules, then the incident or request agent with 12 tools |
| routing | none | orchestrator: rules, then the model, default `incident` |
| retrieval | one search on the researcher's words | the same, plus a second search with the expansion terms, plus the agents' searches; up to 8 sources |
| sources in the prompt | as rc-kg formats them | packed by rank to 7,000 characters |
| playbooks | triggered by rules | triggered by rules, or loaded by an agent (`load_skill`) |
| model calls per draft | 1 (+ writer fallbacks, + background judge) | 1 to 10 (+ the same) |
| review before the engineer sees it | none in the loop (assertions and the judge are after) | critic, guardrails, reviewer agent; one revision |
| input guardrails | none | control tokens removed, injection flagged |
| output guardrails | none | credentials redacted; destructive commands, leaked work notes, unsourced timelines |
| tool calls on the model server | not needed | needed for the agents; without them the agents stop and the writer drafts |
| run log | as chapter 10 | plus `trace.agent`, `trace.guardrails`, the manifest |
| metrics row | `mode: pipeline` | plus route, tools, reviews, guardrails, manifest |
| prompts | `app/prompts.py` | the same writer prompt (plus the revision block), and 9 agent prompt files with versions |
| evaluation | rc-kg's evals | plus `evals/run.py` (rag, routing, guardrails, e2e), `prompt_ab.py`, `replay_eval.py` |
| release gate | the test suites, run one by one | `tests/run_all.sh` |
| OOD app | *RC Copilot*, `~/ondemand/dev/rc-copilot` | *RC Agent*, `~/ondemand/dev/rc-agent` |

### `AGENT_MODE=false` is not TicketMate

`AGENT.md` says "`AGENT_MODE=false` is exactly TicketMate". In the code,
`AGENT_MODE=false` skips the graph only. These still run in rc-agent with
the agent off:

- the input guardrails (they can change the ticket text: control tokens
  out, an injection note in front);
- `retrieve_multi` (a second search when the expansion has terms; up to 8
  sources);
- `context_pack.pack` (sources packed to `KG_CONTEXT_MAX_CHARS`);
- the output guardrails (redaction and caveats).

The writer's `prompt_version` also differs from TicketMate's for the same
writer prompt, because `tracing.prompt_version` hashes every upper-case
string constant in `app/prompts.py`, including `REVISION_TEMPLATE` and the
unused agent prompts. Compare versions within one app, not across.

For a true TicketMate baseline, run TicketMate. For the agent's effect alone,
compare rc-agent with `AGENT_MODE=true` against `AGENT_MODE=false`.

## 18.4 What is shared

| part | how |
|---|---|
| rc-kg code | identical trees. `tests/run_all.sh` stage 10 diffs rc-agent's `rc-kg/` against TicketMate's and fails on any difference: "both apps must run the same knowledge-graph code (they publish to one shared store)" |
| the published graph | one store, `RC_KG_DATA_ROOT` (default `/projects/rc/projects/rc-kg-data`): `graph/` and `graph-website/` snapshots and the build lock. "One published graph serves every session of both apps" (`rc-kg/README.md`). A session of either app switches to a newer snapshot, and rebuilds one older than 24 hours, for both |
| playbooks | the same private directory (`PLAYBOOK_DIR`, set by `cluster/env.sh` to `/projects/rc/projects/rc-playbooks` when it exists); the same parser |
| the extension | the same code apart from one display line; the same name ("RC Copilot for ServiceNow"); either backend's session card configures it |
| the OOD app template | `form.yml`, `submit.yml.erb`, `template/script.sh.erb`, `view.html.erb` are identical; only the manifest's name differs |
| toolchain and caches | rc-agent's `.uv` and `.cache` are symlinks to TicketMate's: the same `uv` Python, vLLM install and model weights |
| backend modules | `auth.py`, `models.py`, `evaluators.py`, `tracing.py`, `runlog.py` (code), `ops_store.py`, `otel.py`, `startup.py`, `ticket_context.py` (except the expansion), the cleaning, lookups and citations |
| the trace store | the same layout under each repository's `traces/$USER/`; `ops/ops_report.py` is the same file |
| the API | the same `POST /api/draft`, `/api/feedback`, `/api/health`; the response adds `context.agent` in rc-agent |
| environment variables | the same names; rc-agent reads the agent ones too |

One consequence of sharing: `ood-connect.js` reads the first
`rc-copilot-connection` marker on the OOD page, and both apps' cards render
that marker with `data-rc-app="RC Copilot"`. With sessions of both running,
check which node the banner names, or paste the URL and token into the
popup.

## 18.5 Cost

### TicketMate, measured

From TicketMate's 77 successful run logs (`traces/<user>/runs/`,
2026-10-23 to 2026-10-27, Qwen2.5-7B-Instruct-AWQ), each a single writer
call:

| GPU | drafts | latency, median | in the model, median | prompt tokens, median | completion tokens, median |
|---|---|---|---|---|---|
| H200 | 53 | 1.0 s | 0.74 s | 5,931 | 148 |
| T4 | 16 | 13.4 s | 13.1 s | 4,498 | 369 |

(8 more have no GPU recorded.) On a T4 the model call is nearly all of a
draft's time. Retrieval took a median 106 ms.

### rc-agent, from the code

No rc-agent draft on a GPU has been recorded yet (`traces/<user>/runs/` is empty), and there
is no `e2e` report, so its latency is not measured. What the code fixes:

| | extra model calls | bound |
|---|---|---|
| orchestrator | 0-1 | 80 output tokens |
| plan | 0-1 | 200 output tokens |
| reasoning turns | 0-4 (`AGENT_MAX_STEPS`) | 600 output tokens each; the prompt grows with every tool result (1,500 chars each) and carries the tool schemas |
| writer revision | 0-1 (`AGENT_MAX_REVISIONS`) | a full writer call |
| reviewer | 0-2 (one per pass) | 500 output tokens; up to ~13.5k characters of evidence plus the draft |
| tool calls | 0-8 (`AGENT_MAX_TOOL_CALLS`) | each lookup up to the 5 s lookup timeout; calls in one turn run concurrently |
| second retrieval | 0-1 | one rc-kg `/retrieve` |

| route | model calls (without writer fallbacks and judge) |
|---|---|
| follow-up (no specialist, no reviewer), no revision | 1 -- as TicketMate |
| request, rules-routed, plan says nothing more needed, review passes | 3 (plan, writer, reviewer) |
| model-routed, full step budget, revised | 10 |

Each call is serialised: the graph is a chain (orchestrator -> agent ->
write -> review). Model calls are where a draft's time goes on a T4 (above),
so time grows roughly with the number of calls; how much depends on
prompt and output lengths that have not been measured for the agents. To
measure: `evals/run.py --suite e2e` (latency and tokens per case),
`tests/agent/replay_eval.py` with `AGENT_MODE=true` and `false` on the same
tickets, then `ops/ops_report.py` (latency by stage) on live use.

`trace.tokens` and the metrics row's `tokens` count the writer's calls
only; the agents' token usage is in `trace.agent.usage` (chapter 17.2).
Compare both when comparing costs.

## 18.6 Which to use when

| situation | use | why |
|---|---|---|
| RC members answering tickets today | TicketMate | the published release; one model call; measured latency |
| a T4 session | TicketMate | each extra call costs seconds there; rc-agent's latency on a T4 is unmeasured |
| the model server has no tool calls (`QWEN_TOOLS=0`, or a model without a parser) | TicketMate, or rc-agent knowing the agents will stop | rc-agent then drafts from the rules' evidence, still paying for the orchestrator and reviewer |
| tickets whose facts the rules do not reach (a user's queue or FairShare when only the job is named, a procedure the triggers missed, documentation in other words) | rc-agent | the agents' tools and reflection exist for this |
| wanting a check before the engineer reads it (claims of actions, redirects, unsupported facts, destructive commands, credentials) | rc-agent | critic, reviewer and guardrails are in the loop |
| changing a prompt, skill, tool or retrieval setting | rc-agent | the ground truth, gates, prompt A/B and `tests/run_all.sh` are here |
| changing rc-kg | either, then copy to the other | stage 10 fails until both trees match |
| comparing the agent with the pipeline | rc-agent with `AGENT_MODE=true` vs `false` | same code otherwise (18.3) |

Agent-ready was a design principle of TicketMate (chapter 1.5): the lookups,
retrieval and playbooks were self-contained functions, which is why rc-agent
could wrap them as tools unchanged. A change to one of them in TicketMate
should be carried to rc-agent; the reverse holds for rc-kg, which the gate
enforces.
