# RC Copilot

**Draft replies to ServiceNow tickets using a private LLM running inside your
own HPC allocation.** Built for the Research Computing team at Northeastern.

One click on a ticket reads it, sends it to a model running in your Slurm job,
and writes a draft into the work notes for you to review. No ticket data leaves
the university, and no ServiceNow API access is needed.

> **Status:** working prototype, end to end. A real model has produced a
> schema-valid draft on the cluster. Draft *quality* is now the open problem —
> see [Status](#status).

---

## The problem

RC staff answer a lot of tickets that rhyme: out-of-memory kills, quota limits,
module and environment mistakes, access requests. Writing each reply from
scratch is repetitive.

Two constraints shaped the whole design:

- **No ServiceNow API access.** Org policy doesn't grant it, so anything that
  reads a ticket has to work from what's already on screen.
- **Ticket text can't leave Northeastern.** It contains researcher names and
  unpublished work, which rules out any hosted LLM API.

Together they force an unusual shape: the **browser** is the integration point,
and the **model runs in the user's own Slurm allocation**.

---

## How it works

![RC Copilot system architecture](docs/rc-architecture.png)


1. You open a ticket and click **Draft reply**.
2. The extension reads the ticket and offers a box for anything the model should
   know that the ticket doesn't say.
3. It goes through Open OnDemand's authenticated proxy to a FastAPI service in
   your Slurm job, which prompts a GLM model served by vLLM.
4. You get back a summary, suggested steps, a confidence score, caveats, and a
   ready-to-edit draft — written into **work notes**, unsaved.

**Nothing is ever submitted automatically.** A human reviews and clicks Update.

### Grounded in the RC website

Each ticket is also matched against the rc.northeastern.edu website and the
rc-docs.northeastern.edu documentation, served from a Neo4j knowledge graph by
[rc_kg](../rc_kg) in the same job. The most
relevant sections go into the prompt as numbered sources; the model cites them
as `[S1]`, and the backend turns those into a numbered **References** block in
the reply (and a Sources line in the steps) with the exact page links. The
model never writes a URL: one it writes anyway is checked against what was
retrieved and removed if it is not there, with a caveat saying so.

rc_kg starts automatically with the session and needs no setup beyond a graph
snapshot (`/projects/rc/projects/rc_kg/scripts/build_graph.sh`, run once). If
it is missing or still loading, drafts work exactly as before, with a caveat
that they were not checked against the website. `/api/health` reports it as
`knowledge_graph`.

Requires the rc_kg HTTP API **1.x** (tested against rc_kg `v1.1.0`; see its
`API_CONTRACT.md`). The backend checks `api_version` and, on a different major
version, drafts ungrounded with a caveat naming both versions rather than
misreading fields. `tests/rc_kg/run_contract.sh` (no GPU, ~2 min) checks the
integration end to end: grounding and citations against a real rc_kg, an
incompatible rc_kg refused, and rc_kg down.

---

## Quick start

**1 — Launch the backend.** In Open OnDemand: *Develop → My Sandbox Apps →
RC Copilot → Launch*. It defaults to a small 3B model on any free GPU, which
schedules in seconds; switch to GLM-4.7-Flash once you're happy it works.

**2 — Install the extension.** Download
[`dist/rc-copilot-extension.zip`](dist/rc-copilot-extension.zip),
unzip it, then `chrome://extensions` → Developer mode → **Load unpacked** →
select the folder.

**3 — Open OOD's "My Interactive Sessions".** The extension picks up the
address and token on its own; there's nothing to copy.

**4 — Open a ticket** and click **Draft reply**.

### Without the browser at all

The fastest loop for tuning the prompt needs neither the extension nor OOD:

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --port 8080
```

Paste tickets at `http://127.0.0.1:8080` and edit
[`app/prompts.py`](backend/app/prompts.py).

---

## Layout

| Path | What it is |
| --- | --- |
| [`backend/`](backend) | FastAPI service — prompt, vLLM client, auth. Knows nothing about ServiceNow |
| [`extension/`](extension) | Chrome MV3 extension |
| [`ood-apps/rc-copilot/`](ood-apps/rc-copilot) | OOD interactive app: vLLM + backend in one job |
| [`ood-apps/capture-test/`](ood-apps/capture-test) | Same, no model — stores what's sent, for testing the data path |
| [`cluster/`](cluster) | Slurm launchers, install and diagnostic scripts |
| [`tests/`](tests) | Standalone inference test — run it on the cluster, no browser or OOD needed |
| [`dist/`](dist) | Packaged extension zip |
| [`docs/`](docs) | Diagram and extension guide |

Helper scripts live in `cluster/`: `install_vllm.sh --check`, `check_gpus.sh`,
`vllm_status.sh`, `smoke_test.py`.

---

## A few things worth knowing

**The structured output is validated, not trusted.** The model returns JSON;
three strategies are tried (constrained decoding → JSON mode → prompt-only) and
whatever comes back is parsed tolerantly and checked against a schema. The
high/medium/low confidence thresholds are applied *server-side* — the model
supplies a number, we decide what it means.

**The extension can't fetch from the page.** An MV3 content script runs in the
page's origin and is subject to CORS, so all network I/O goes through the
service worker, where `host_permissions` applies instead.

**Three ServiceNow UIs, one content script.** Classic puts the form in an
iframe, Next Experience hides fields in shadow DOM, Service Portal uses its own
id convention. Rather than three sets of selectors, fields are found by a
shadow-piercing DOM walk matched on id, name, `aria-label` and label text.

**Drafts go to work notes by default**, not to customer-visible comments — so a
mis-click on Update can't send unreviewed model output to a researcher. Two
other targets can be chosen in the toolbar popup: *additional comments*, and
*both* (analysis to work notes, reply to comments). Nothing is ever saved
either way; a human still clicks Update.

---

## Status

**Proven**

- Full data path with real tickets: ServiceNow → extension → OOD → Slurm job →
  disk, including ticket-number extraction
- Auth against tampered signatures and forged expiries
- The OOD proxy path, field matching across all three UIs, partition/GPU limits

- **Real inference on the cluster.** Qwen2.5-3B on a T4 (`d1025`) produced a
  schema-valid draft from the real prompt in 13.9s — summary, 5 steps,
  confidence 0.80, 4 caveats, a 729-character reply

**Not yet good enough**

- **Draft quality.** The first real run exposed three prompt defects: the model
  copied caveats verbatim from the prompt's example list (3 of 4 were
  contradicted by the ticket), signed off `[Your Name]`, and gave generic
  "check your versions" advice where the error text already named the fault.
  All three are now addressed in `prompts.py` and need re-testing
- The GLM stack hasn't completed a run on a GPU node — only the 3B model has

**Next step:** re-run `tests/` with the revised prompt, then work through 20–30
real tickets, tuning [`app/prompts.py`](backend/app/prompts.py) until the drafts
are worth sending.

---

## Documentation

| Document | For |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full design: components, security model, design decisions, deployment |
| [VLLM_SETUP.md](VLLM_SETUP.md) | Model layer: which GLM fits A100 hardware, and the CUDA install trap |
| [docs/EXTENSION.md](docs/EXTENSION.md) | Extension + OOD apps in detail |
| [backend/README.md](backend/README.md) | Backend configuration and API |

---

## Caveats

This is a prototype built against one institution's setup. Hostnames, partition
names and GPU types are Northeastern-specific and would need changing elsewhere.

Every draft is a starting point. The model can be confidently wrong — that's why
`caveats` is a first-class field in the response and is shown but deliberately
never written into the ticket.
