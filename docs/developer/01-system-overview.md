---
name: 1. System overview
route: /system-overview
menu: Foundations
---

# 1. System overview

## 1.1 The problem

RC answers a steady stream of ServiceNow tickets about the Explorer HPC
cluster. Many are recognisable -- a job killed for memory, a quota error, a
module mistake, a request for access to a group, a partition or AICR -- and
each still takes an engineer time to diagnose and write up. RC Copilot drafts
the reply; an engineer reviews, edits and sends it. **Nothing is ever sent
or submitted automatically.**

## 1.2 Constraints that shape everything

| constraint | consequence |
|---|---|
| No ServiceNow API access (organisational policy) | The browser is the integration point: an extension reads the ticket from the page the engineer is looking at and writes the draft back into it |
| Ticket text cannot leave Northeastern (researcher names, unpublished work) | No hosted model APIs. The model runs on Explorer, **inside the engineer's own Slurm job**, with everything else that touches ticket text |
| The engineer's job is a GPU node reachable by every cluster user | Only the backend listens on the network, behind OOD's SSO and an access token; the model and the knowledge graph listen on loopback with per-session credentials |
| Small models (Qwen2.5-7B, 4-bit, on a 16 GB T4) | Every piece of context is engineered: cleaned, labelled, budgeted, ordered; output is constrained JSON; rules a model might break are checked in code |
| Answers must be RC's, not the internet's | Replies are grounded in RC's own website and documentation (the knowledge graph), with exact links; the model never writes a URL |

## 1.3 Components

```
  Chrome (RC engineer)                Open OnDemand               Slurm job on a GPU node
 ┌──────────────────────────┐      ┌──────────────────┐      ┌──────────────────────────────────┐
 │ ServiceNow ticket page   │      │ SSO + node proxy │      │ backend (FastAPI)  0.0.0.0:PORT  │
 │  ▲ draft into work notes │ HTTPS│ /rnode/<n>/<p>   │      │   ├──► vLLM          127.0.0.1   │
 │ extension                ├─────►│                  ├─────►│   ├──► rc-kg API     127.0.0.1   │
 │  content.js  reads page  │      │                  │      │   │      └─ Neo4j  (node-local)  │
 │  background.js all fetch │      │                  │      │   └──► id/sacct/sinfo/... (read) │
 └──────────────────────────┘      └──────────────────┘      └──────────────────────────────────┘
```

| component | language | where | role |
|---|---|---|---|
| Extension | JavaScript (Chrome MV3) | `ticketmate/extension/` | reads the ticket, shows the panel, writes the draft into a field |
| OOD app | ERB/bash | `ticketmate/ood-apps/rc-copilot/` | an Open OnDemand interactive app that starts the whole stack in one GPU job and publishes its address and token on the session card |
| Backend | Python 3.12, FastAPI | `ticketmate/backend/app/` | the draft pipeline; also serves a paste-a-ticket web page (`backend/web/`) |
| vLLM | Python | `ticketmate/cluster/serve_glm.sh` | serves the model with an OpenAI-compatible API |
| rc-kg | Python, Neo4j 5.26 | `rc-kg/` | builds the knowledge graph from the RC website and rc-docs; serves hybrid retrieval, citation rendering and support checks over HTTP |

## 1.4 One draft, end to end

What happens when an engineer clicks **Draft reply** (TicketMate). Each step
names the code that does it; chapters 3-10 go into each.

| # | step | code |
|---|---|---|
| 1 | **Read** the ticket number, short description, description, a catalog request's form answers and the activity stream from the page | `extension/content.js` |
| 2 | **Send** `POST /api/draft` through OOD's node proxy, with the OOD SSO cookie and a signed session | `extension/background.js` |
| 3 | **Clean** the scraped page: notifications, address lists, signatures, quoted chains, ServiceNow's auto-acknowledgement out; every comment and work note kept, oldest first, each author labelled *RC staff* or *researcher* | `ticket_context.prepare` |
| 4 | **Build the retrieval query** from the researcher's own words: subject, description, latest message | `ticket_context.build_query` |
| 5 | **Look up** what the ticket names, read-only, as the engineer: accounts, jobs, groups, nodes, partitions, queue, FairShare, project owners, libraries | `lookups.run` (concurrent with 6) |
| 6 | **Retrieve** six numbered sources `[S1]..[S6]` from the knowledge graph | `kg_client.retrieve` -> rc-kg `/retrieve` |
| 7 | **Match playbooks**: RC's procedures for this kind of request, with values found in the ticket and what is missing | `playbooks.find` |
| 8 | **Fit** the ticket into what the model's context window leaves | `ticket_context.fit` |
| 9 | **Prompt**: system rules, ticket, lookups, sources, workflow, instructions, checklist | `prompts.build_messages` |
| 10 | **Generate** a JSON draft (constrained decoding with fallbacks), validated by Pydantic | `llm_client.generate_draft` |
| 11 | **Cite**: `[S#]` markers become numbered links; any URL not retrieved is removed; citations that a better source supports are flagged | `kg_client.apply_citations` -> rc-kg `/cite` |
| 12 | **Evaluate**: code assertions of the prompt's rules, metrics, and (in the background) an LLM judge | `evaluators` |
| 13 | **Record** the full trace: run tree, prompts, completions, tokens, timings | `runlog.record`, `tracing` |
| 14 | **Show**: the panel writes the draft into work notes (default), unsaved, with *Check before sending*, steps, lookups, cited pages, checks and feedback | `content.js` |

> **rc-agent:** between steps 8 and 10 an agent graph runs: an orchestrator
> routes the ticket to an incident or a request agent, which calls read-only
> tools for anything else the reply needs; after the draft, a critic,
> guardrails and a reviewer agent check it and may send it back once.
> Guardrails also run on the input (step 3) and the final draft (after 11);
> retrieval adds a second search in documentation vocabulary (step 6);
> sources are packed by rank (step 9); every agent step is a span in the
> trace (step 13). Chapters 15-17.

## 1.5 Design principles

These recur in every chapter; when changing the code, keep them.

1. **The model never acts.** It drafts text. Everything it could get
   dangerously wrong is done in code: commands it never chooses, URLs it
   never writes, actions it may not claim.
2. **Code enforces, the prompt explains.** A rule in the prompt that matters
   (sign-off, no invented ticket number, cite only what a source says) also
   has an assertion that checks it.
3. **Measure, then change.** Thresholds and weights in the code carry a
   comment with the measurement that chose them (date, data, numbers). A
   change without a measurement is a guess.
4. **Degrade, never fail.** The knowledge graph down, a lookup timing out, a
   model server without guided decoding or tool calls: the draft is still
   produced, with a caveat saying what was missing.
5. **The engineer's judgement is the ground truth.** The in-loop LLM judge is
   the same model grading itself; reviewed tickets become regression cases.
6. **Agent-ready.** Every capability is a self-contained function with plain
   inputs and outputs, so it can be a tool of an agent unchanged (which is
   how rc-agent was built).
