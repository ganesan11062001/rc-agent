---
name: Developer guide
route: /
menu: Start here
---

# RC Copilot developer guide

RC Copilot drafts replies to Research Computing (RC) ServiceNow tickets. A
Chrome extension reads the ticket from the page; a backend in the RC
engineer's own Slurm job cleans it, looks things up on the cluster,
retrieves RC's documentation from a knowledge graph, asks a locally served
model for a draft, checks it, and returns it to the page, unsaved, for a
person to review.

There are two codebases:

| | path | what it is |
|---|---|---|
| **TicketMate** | `/projects/rc/projects/ticketmate` | the product as RC members use it: a fixed pipeline -- rules gather the facts, one model call writes the draft |
| **rc-agent** | `/projects/rc/projects/rc-agent` | a copy of TicketMate in which, between gathering and writing, a LangGraph multi-agent system decides what else to look up, and reviewers check the draft; plus guardrails, evaluation and LLMOps |

Both repositories carry this same guide, in `docs/developer/`. Both contain
**rc-kg**, the knowledge graph. Everything in chapters 1-14 is true of both;
where rc-agent differs, a box says so:

> **rc-agent:** like this.

## Reading order

Read chapters 1 and 2 first; after that, any chapter stands on its own.
Chapters 15-17 describe code that exists only in rc-agent; chapter 18 compares
the two.

| # | chapter | you will know |
|---|---|---|
| 1 | [System overview](01-system-overview.md) | the constraints, the components, one draft end to end |
| 2 | [Repository and environment](02-repository-and-environment.md) | where everything is, the Python environments, how to run it |
| 3 | [The Chrome extension](03-extension.md) | how the ticket is read and the draft written back |
| 4 | [The backend request pipeline](04-backend-pipeline.md) | `POST /api/draft` line by line; models, auth, startup |
| 5 | [Ticket context](05-ticket-context.md) | cleaning, RC staff vs researcher, the retrieval query, fitting |
| 6 | [Cluster lookups](06-cluster-lookups.md) | the read-only commands and why they are safe |
| 7 | [Playbooks (skills)](07-playbooks.md) | RC's procedures as files: triggers, stages, missing information |
| 8 | [Prompts and generation](08-prompts-and-generation.md) | prompt assembly, structured output, the model client |
| 9 | [The knowledge graph (rc-kg)](09-knowledge-graph.md) | ingest, schema, enrichment, hybrid retrieval, the API, snapshots |
| 10 | [Citations, evaluation, tracing](10-citations-evaluation-tracing.md) | how links are made safe, per-draft checks, run logs and run trees |
| 11 | [Security and privacy](11-security-privacy.md) | the threat model and every control |
| 12 | [Testing](12-testing.md) | each test suite and what it proves |
| 13 | [Recipes](13-recipes.md) | add a playbook, a lookup, a topic; debug a bad draft; rebuild the graph |
| 14 | [Configuration reference](14-configuration.md) | every setting, with defaults |
| 15 | [rc-agent: the agent graph](15-agents.md) | orchestrator, ReAct specialists, tools, skills, state, reviewers |
| 16 | [rc-agent: guardrails and context engineering](16-guardrails-context.md) | input/output guardrails, multi-query retrieval, context packing |
| 17 | [rc-agent: LLMOps](17-llmops.md) | trace store, OpenTelemetry, metrics, ground truth, evals, optimization, release gate |
| 18 | [TicketMate vs rc-agent](18-ticketmate-vs-rc-agent.md) | every difference, and how to choose |
| 19 | [Sign-off, request forms, the LLM judge, readable traces](19-quality-loop.md) | the engineer's sign-off, per-request-type extraction, the judge gate and rubric, the .md beside every run log |

## Other documents

These stay the reference for their subjects; this guide explains and links to them.

- `README.md` (repo root) -- what the project is, quick start
- `ticketmate/ARCHITECTURE.md` -- the architecture summary and design decisions
- `ticketmate/docs/EXTENSION.md` -- extension and OOD app operations
- `ticketmate/backend/README.md` -- backend setup and environment variables
- `ticketmate/VLLM_SETUP.md` -- getting vLLM to run on Explorer's GPUs
- `ticketmate/ood-apps/MEMBERS.md` -- the getting-started guide for RC members
- `rc-kg/README.md`, `rc-kg/API_CONTRACT.md` -- the knowledge graph and its API
- rc-agent only: `ticketmate/AGENT.md`, `ticketmate/LLMOPS.md`

## Viewing this guide as a site

The chapters are plain Markdown with [Docz](https://www.docz.site/)
frontmatter (`name`, `route`, `menu`). With Node.js available:

```bash
cd docs/developer && npx docz dev        # or: npx docz build
```

Any Markdown viewer (GitHub, VS Code preview, MkDocs) reads them as they are.
