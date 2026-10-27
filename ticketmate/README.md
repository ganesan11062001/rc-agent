# RC Copilot (TicketMate)

**Draft replies to ServiceNow tickets with a private LLM running inside your
own HPC job.** Built for the Research Computing team at Northeastern.

One click on a ticket reads it, sends it to a model in your Slurm job, and
writes a draft into the work notes for you to review. The draft is grounded in
the RC website and rc-docs, checked against live cluster facts (accounts,
jobs, nodes), and comes with what to verify before sending. No ticket data
leaves the university, and no ServiceNow API access is needed. **Nothing is
ever submitted automatically** — you review, edit and click Update.

- **New here?** Follow [ood-apps/MEMBERS.md](ood-apps/MEMBERS.md): install
  once, then launch, draft, review.
- **How it works inside:** [ARCHITECTURE.md](ARCHITECTURE.md).
- **Setting up a checkout or developing:** the
  [repository README](../README.md#running-it).

![RC Copilot system architecture](docs/rc-architecture.png)

---

## What happens when you click Draft reply

1. The extension reads the ticket from the page — short description,
   description, comments and work notes — plus any instructions you typed.
2. It sends them, through Open OnDemand's SSO proxy, to the backend in your
   Slurm job.
3. The backend **cleans the ticket** (no notification emails, CC lists,
   signatures; thread oldest first, each entry labelled RC staff or
   researcher), runs **read-only cluster lookups** for what the ticket names,
   and **retrieves** the most relevant RC website and rc-docs sections.
4. The model (Qwen2.5-7B AWQ by default; GLM-4.7-Flash on an 80 GB GPU)
   writes a summary, steps for you, a confidence score, caveats and the reply;
   citations become exact links.
5. The draft lands in **work notes** (or the field you chose), unsaved. The
   panel shows *Check before sending* and the draft; steps, lookups, cited
   pages, quality checks and a feedback form are one click away.

---

## Features in detail

### Ticket context: what the model actually reads

The extension sends the whole ticket page; the backend cleans it before
retrieval, lookups or the model see it (`backend/app/ticket_context.py`):

- **Removed:** "Email sent" notifications (copies of comments already in the
  thread), every `To:`/`Cc:`/`From:` line and address list (the RC team is
  CC'd on almost every ticket), "Show email details" and avatar initials,
  signatures after a dashed rule, quoted reply chains, and ServiceNow's
  automatic acknowledgement.
- **Compressed:** "Field changes" to one line (`Incident state: New -> In
  Progress`); "Email received" to its subject.
- **Kept, word for word:** every comment and work note, oldest first, headed
  `[date time] Author, role (comment, visible to the researcher | internal
  work note)`. Internal notes must not be revealed to the researcher, and the
  prompt says so.
- **Labelled:** each author as `RC staff` or `researcher`, so the model reads
  the newest staff entries as what has already been done.

**Retrieval searches the researcher's words, not the thread:** the short
description, the description and the researcher's latest message. Searching
the whole thread matched RC's own wording instead (the acknowledgement pulled
training pages into 15 of 19 tickets). A forwarded email with no words of its
own ("Copying rchelp.") keeps the quoted request it forwards, for the model
and for retrieval. When your instruction asks for a follow-up or a closure and
not for a page or how-to, the website sources are left out of the prompt
(still retrieved, tagged `kg:sources_not_needed`): in 40 of 41 such runs none
was cited, and their ~2k tokens are better spent on the thread.

On 18 real tickets cleaning halves the text (73,793 → 37,449 characters) and
removes ~35 addresses per ticket. The result then has to fit: the backend
reads the model's context window from vLLM and gives the ticket what is left
after the system prompt, sources, lookups and the reply, at most
`TICKET_MAX_TOKENS` (6,000). A longer thread keeps its description, first
entry and newest entries, with a marker saying how many were left out. Under
each draft: `ticket 14,210 → 3,950 chars · removed 9 notification emails, 35
addresses`.

### Cluster lookups (read-only)

The backend looks up what the ticket names, on the cluster, as you, inside
your job:

| ticket mentions | command | what the model gets |
|---|---|---|
| a username (`doe.l`) | `id doe.l` | the account exists, and its groups |
| a job ID (`job 4812345`, `slurm-4812345.out`) | `sacct -j 4812345 --parsable2 ...` | state, exit code, run time and limit, memory requested and used, partition, node |
| a group (`the gaussian group`) | `getent group gaussian` | its size, and whether the ticket's users are in it |
| a node (`d4043`) | `scontrol show node d4043` | state, partitions, GPUs, CPUs/memory in use, Slurm's reason if down or drained |
| a partition (`-p short`, `nodes on ce-mri`) | `sinfo -h -N -p ce-mri -o %N\|%T\|%E` | node counts by state, each down or drained node with its reason |
| a job that was found | `sacct -j 4812345 --batch-script`; `squeue -j ... --start` / `-O TimeLeft` | its `#SBATCH` lines and commands (first 2 jobs); for a pending job the expected start and reason, for a running one the time left |
| a user, when the ticket says jobs are pending / not starting | `squeue -u <user>`; `sshare -a -u <user>` and `-A <their account>` | their jobs with start times and reasons; FairShare (flagged under 0.5), the group's size and the heaviest member's share of usage, never other members' names |
| a user, when the ticket says jobs fail / crash / are killed | `sacct -u <user> -S now-7days -X` | counts by state, and the jobs that did not complete |
| a project (`/projects/smithlab`) | `stat -c %U\|%G /projects/smithlab` | who owns it (the PI, usually); nothing if this node cannot see the owner |
| a library (`libcudnn.so.8`) | `ldconfig -p`, filtered | whether it, or another version of it, is in the system library cache |

Groups are also looked up in `/shared/admin/ROOT/etc/group.xtra` (a file
read). Taken from the team's Slurm cheat sheet; its commands that change
things (`scontrol update`, node drain/resume, `project ... add members`) are
not here and cannot be.

**Lookups only.** Those nine programs are the only ones that can run, with
fixed arguments, exec'd without a shell; an argument from the ticket must
fully match a strict pattern or it is dropped; email addresses are never
usernames. At most 4 users, 3 jobs, 2 groups, 3 nodes, 2 partitions, 2
projects and 3 libraries per draft, queue/FairShare/history for 2 users, 5 s
each. The panel lists every command and its result; the run log
keeps the output. Off with `CLUSTER_LOOKUPS=false`; tested by
`tests/lookups/test_lookups.py`, including that injection text runs nothing.

### RC workflow playbooks

Some requests are not answered from the website: RC has a procedure for them.
Each procedure is a Markdown runbook in `backend/app/playbooks/`, with a
header saying which tickets it is for:

| playbook | for |
|---|---|
| `aicr-access` | adding/removing someone on an AICR project, closing one (`aicr-access.bash`, PR, `aicr_neu.csv` to the AICR support address) |
| `multigpu-access` | multi-GPU partition access: memory vs scaling, 24 h test reservation, verifying the benchmark (`gpu-logs` > 10% per GPU), granting |
| `long-partition` | long partition access: the checkpointing question, granting |
| `explorer-access` | Explorer access through a research group: PI self-service, adding with the owner's approval |
| `gaussian` | the Gaussian group, and how to start with Gaussian |
| `storage-extension` | more /projects storage (the PI's extension form; 35 TB free) |
| `course-request` | a course on Explorer (explorer-courses PR, starter email; no rosters, groups or reservations) |
| `pending-fairshare` | jobs pending for a long time (FairShare is per PI group) |

When the researcher's words match a playbook (RC's own replies never do), its
runbook and the values found in the ticket (emails, project ids, usernames,
the ticket number) go into the prompt. The model works out which stage the
ticket is at from the RC staff entries -- an AICR request with a PR link in a
work note is waiting for review, not new -- and writes that stage's steps,
with the values filled in, and that stage's reply. **Nothing is run:** the
script, reservations and group changes are steps for you. The panel's
context line and the run log name the playbook used (`playbook:<name>` tag).

**Missing information is asked for.** Each playbook lists what RC needs
before it can act (`require.<field>: what to ask for`; e.g. AICR needs each
person's email, the project number and its year). When the ticket gives none
of a requirement's fields, the prompt marks it MISSING and the checklist
names it: the reply asks the researcher for exactly those items, as a short
list, and does not say the request is being processed yet. The model first
checks whether the ticket gives it another way (a project's title instead of
its number), makes it a step instead when RC can find it (a username from an
email), and never asks researchers for their own username or email. Drafts
that asked are tagged `playbook:missing_info`.

To add or change a procedure, edit or add a file and restart the backend;
`tests/playbooks/test_playbooks.py` checks that every file parses and routes
the right tickets (add a case for a new one). Off with `PLAYBOOKS=false`.

### Grounded in the RC website and rc-docs

Each ticket is matched against rc.northeastern.edu and
rc-docs.northeastern.edu, served from a Neo4j knowledge graph by
[rc-kg](../rc-kg) in the same job. The six most relevant sections go into the
prompt as numbered sources; the model cites them as `[S1]`, and the backend
turns those into a **References** block with exact page links. The model never
writes a URL: one it writes anyway is checked against what was retrieved and
removed if it is not there.

rc-kg starts with the session and loads its graph in 1–2 minutes; until then,
or if it is down, drafts are written ungrounded with a caveat saying so.
`RC_KG_VARIANT=full` (website + rc-docs, default) or `website`; on the same 45
questions `full` finds the right page in the top 6 for 98% vs 69%
([evidence](../rc-kg/README.md#graph-variants)). The backend requires rc-kg's
HTTP API **1.x** and drafts ungrounded rather than misreading a different
major version.

### Every draft is traceable

Each draft writes one private record,
`/scratch/$USER/rc_copilot_runs/<ticket>/<time>-<id>.json` (mode 600 in a
mode-700 directory), modelled on LangSmith with no external service: a tree of
runs (cleaning, lookups, retrieval and rc-kg's own stages, each model call,
parsing, citations, evaluation), the exact prompts, tokens and timings, and the
versions — model, prompt version (a hash of every template), TicketMate commit,
rc-kg API and graph build. `cluster/traces.py` reads them:

```bash
cluster/traces.py list                                  # newest drafts: tokens, latency, grounding
cluster/traces.py show <request_id> [--io] [--prompt]   # the run tree, inputs/outputs, exact prompts
cluster/traces.py runs --type llm --status error        # runs across all drafts, filtered
cluster/traces.py stats --by prompt_version             # per-step p50/p95, tokens, errors, feedback
cluster/traces.py export drafts.csv                     # one row per draft, no ticket text
```

rc-kg logs only request ids, timings and counts — never ticket text. `/scratch`
is purged monthly.

### Evaluation of every draft

| metric | how | when |
|---|---|---|
| **assertions** | code checks of the prompt's rules: exact sign-off, no placeholder name, no invented ticket number, no promised timeline, confidence consistent with caveats, short summary, has steps, no URLs outside the sources, cites the sources when they were good (not when you gave instructions) | instant, shown in the panel |
| **hallucination, correctness** | LLM judge against the sources and the ticket; invented URLs removed; people's *Correct / Partly / Wrong*; similarity between the draft and the reply actually sent | background; any time |
| **conciseness, relevance** | reply length against a target; embedding similarity of ticket and reply | instant |
| **user satisfaction** | 👍/👎, 1–5, *used as is / edited / not used*, comment | the feedback form |

The judge is the serving model grading its own draft (`ONLINE_EVAL`): use it
for trends; people's feedback is the ground truth.

---

## Running it

- **As an RC member:** [ood-apps/MEMBERS.md](ood-apps/MEMBERS.md).
- **Maintainer setup, batch jobs, tests:** the
  [repository README](../README.md#running-it).
- **Prompt iteration without the browser:** run the backend against any vLLM
  endpoint and paste tickets into its web page —
  [backend/README.md](backend/README.md).

A fresh session takes a few minutes to load the model and 1–2 minutes to load
the graph; the panel shows both progress displays (vLLM's loading stage, the
graph's checklist) instead of an error, and drafts as soon as the model is up.

---

## Layout

| Path | What it is |
| --- | --- |
| [`backend/`](backend) | FastAPI service: the draft pipeline, prompts, vLLM client, rc-kg client, lookups, run log, evaluation, auth; plus the paste-a-ticket web page in `web/` |
| [`extension/`](extension) | Chrome MV3 extension: the panel on ServiceNow |
| [`ood-apps/rc-copilot/`](ood-apps/rc-copilot) | OOD interactive app: vLLM + backend + rc-kg in one job |
| [`ood-apps/capture-test/`](ood-apps/capture-test) | Same, no model — stores what is sent, to test the data path |
| `ood-apps/publish.sh`, `install.sh` | Release the app for RC members; link it into a member's sandbox |
| [`cluster/`](cluster) | Slurm launchers, vLLM install, diagnostics, `traces.py` |
| [`tests/`](tests) | Inference, contract, startup, lookups, ticket-context and UI tests |
| [`dist/`](dist) | The packaged extension zip |
| [`docs/`](docs) | Diagram and the extension/OOD guide |
| [`../rc-kg/`](../rc-kg) | The knowledge graph (sibling in this monorepo) |

---

## Documentation

| Document | For |
| --- | --- |
| [ood-apps/MEMBERS.md](ood-apps/MEMBERS.md) | **Getting started** for RC team members: install, launch, draft |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Design: the draft pipeline, components, security, decisions, deployment, tests |
| [backend/README.md](backend/README.md) | Backend configuration, API, running it by hand |
| [docs/EXTENSION.md](docs/EXTENSION.md) | The extension and the OOD apps in detail |
| [VLLM_SETUP.md](VLLM_SETUP.md) | The model layer: models, GPUs, the CUDA install trap |
| [tests/README.md](tests/README.md) | The inference test on a GPU |
| [../rc-kg/README.md](../rc-kg/README.md) | The knowledge graph: build, serve, evaluate |

---

## Status

**Working:** real drafts on real tickets with Qwen2.5-7B AWQ, grounded in the
graph, with lookups, cleaned context and per-draft evaluation; the full path
ServiceNow → extension → OOD → Slurm job and back; auth against tampered
sessions; field matching across all three ServiceNow UIs.

**Being improved:** draft quality, ticket by ticket from reviewed drafts —
each reviewed failure becomes a case in `rc-kg/eval/ticket_cases.yaml`. Open
questions (policy review, distributing the extension, GLM vs Qwen) are in
[ARCHITECTURE.md](ARCHITECTURE.md#11-status-and-open-questions).

Every draft is a starting point. The model can be confidently wrong — that is
why *Check before sending* is shown with every draft and never written into the
ticket. Hostnames, partitions and GPU types are Northeastern-specific.
