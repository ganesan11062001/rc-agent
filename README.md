# TicketMate (RC Copilot)

Drafts replies to Research Computing ServiceNow tickets with a model served in
the RC member's own Slurm job, grounded in the RC website and rc-docs and
checked against live cluster facts. A Chrome extension reads the ticket and
writes the draft into it, unsaved, for a person to review and send.

**Start here:**

| You are | Read |
| --- | --- |
| an RC team member who wants to use it | [ticketmate/ood-apps/MEMBERS.md](ticketmate/ood-apps/MEMBERS.md) — getting started, step by step |
| curious how it works | [ticketmate/ARCHITECTURE.md](ticketmate/ARCHITECTURE.md) |
| maintaining or developing it | this page, then the READMEs below |

```
/projects/rc/projects/ticketmate/     this repository
├── ticketmate/    the app: FastAPI backend, Chrome extension, OOD app, Slurm scripts, tests
├── rc-kg/         the knowledge graph: builds the Neo4j snapshot, serves retrieval over HTTP
├── rc-copilot/    published OOD releases + the extension zip (not in git; written by publish.sh)
├── .uv/           shared Python toolchain (not in git)
└── .cache/        model weights and compile caches (not in git)
```

The two halves talk only over rc-kg's versioned HTTP API
([rc-kg/API_CONTRACT.md](rc-kg/API_CONTRACT.md)). No script hard-codes this
location: each finds the repository from its own path, and the OOD app is told
it when published.

---

## Running it

### Using it (RC team members)

The full guide is [MEMBERS.md](ticketmate/ood-apps/MEMBERS.md). In short:

```bash
/projects/rc/projects/ticketmate/rc-copilot/install.sh     # once: symlinks the app into ~/ondemand/dev
```

1. **Extension, once:** unzip `rc-copilot/rc-copilot-extension.zip` on your
   computer; `chrome://extensions` → Developer mode → **Load unpacked**.
2. **OOD → Develop → My Sandbox Apps → RC Copilot → Launch.**
3. **Open My Interactive Sessions** — the extension configures itself.
4. **Open a ticket, click Draft reply**, review, edit, click Update yourself.

### Setting it up (maintainer, once per checkout)

Everything already exists in this checkout. Re-run a step only when what it
builds has to change.

```bash
cd /projects/rc/projects/ticketmate

# Model server: vLLM 0.29 (+cu129) into ticketmate/.venv. --check changes nothing.
ticketmate/cluster/install_vllm.sh

# Backend: FastAPI into ticketmate/backend/.venv
python3 -m venv ticketmate/backend/.venv
ticketmate/backend/.venv/bin/pip install -r ticketmate/backend/requirements.txt

# Knowledge graph: Neo4j and a venv, then both graph snapshots (~4 min each,
# needs network to rc.northeastern.edu and rc-docs, so a login node works)
rc-kg/scripts/install_neo4j.sh
python3 -m venv rc-kg/.venv
rc-kg/.venv/bin/pip install -r rc-kg/requirements.txt
rc-kg/scripts/build_graph.sh --all

# Release the OOD app and the extension to rc-copilot/ (changes everyone's app at once)
ticketmate/ood-apps/publish.sh
```

Model weights are cached in `.cache/huggingface` the first time a model is
used. Neo4j needs Java 17+; rc-kg finds one itself (the nodes' default `java`
is 11).

### Releasing a change

| What changed | What to do |
| --- | --- |
| backend (`ticketmate/backend/`) or rc-kg code | nothing to publish — sessions run them from this checkout; new sessions pick them up |
| the graph's content (the websites changed) | `rc-kg/scripts/build_graph.sh --all` (eval-gated; keeps `previous/`) — or weekly via `rc-kg/scripts/rebuild.sbatch` from `scrontab` |
| the extension (`ticketmate/extension/`) | bump `version` in `manifest.json`, `ticketmate/extension/package.sh`, then `publish.sh`; tell members to re-download and reload |
| the OOD app templates (`ticketmate/ood-apps/rc-copilot/`) | `ticketmate/ood-apps/publish.sh` |

`publish.sh --list` shows releases; `publish.sh --rollback` steps back one.
**After moving or renaming the checkout, republish**: the published session
script learns the checkout's location from the `ticketmate.env` that
`publish.sh` writes beside it.

### Without OOD

| Goal | Command | Details |
| --- | --- | --- |
| Model + backend + graph in one batch job | `sbatch --export=ALL,MODEL=t4-7b --gres=gpu:t4:1 ticketmate/cluster/serve_stack.sbatch` | [EXTENSION.md](ticketmate/docs/EXTENSION.md) |
| Tune the prompt in the web page, against any vLLM endpoint | `cd ticketmate/backend && .venv/bin/uvicorn app.main:app --port 8080` | [backend/README.md](ticketmate/backend/README.md) |
| Check inference works on a GPU | `sbatch --gres=gpu:t4:1 ticketmate/tests/run_inference_test.sbatch` | [tests/README.md](ticketmate/tests/README.md) |
| Serve the graph on its own | `rc-kg/scripts/serve_kg.sh --port 8090` | [rc-kg/README.md](rc-kg/README.md) |

### Tests

```bash
ticketmate/tests/rc_kg/run_contract.sh      # TicketMate against a real rc-kg, no GPU, ~5 min (consumer contract)
rc-kg/scripts/contract_test.sh              # rc-kg's side of the API contract (provider)
(cd rc-kg && .venv/bin/python -m pytest tests -q)      # rc-kg unit tests
(cd rc-kg && scripts/with_kg.sh -m eval.ticket_cases)  # reviewed-ticket regressions
ticketmate/tests/startup/run_slow_start.sh  # startup progress display, no GPU
ticketmate/backend/.venv/bin/python ticketmate/tests/lookups/test_lookups.py          # read-only lookups
ticketmate/backend/.venv/bin/python ticketmate/tests/ticket_context/test_ticket_context.py  # ticket cleaning
ticketmate/backend/.venv/bin/python ticketmate/extension/check.py                     # extension static checks
ticketmate/tests/ui/run.sh                  # web page and extension panel, headless browser
```

A change to rc-kg's API bumps its version and runs both contract tests. Every
test is described in [ARCHITECTURE.md §10](ticketmate/ARCHITECTURE.md#10-tests).

---

## Documentation

| Document | For |
| --- | --- |
| [ticketmate/ood-apps/MEMBERS.md](ticketmate/ood-apps/MEMBERS.md) | Getting started (RC team members) |
| [ticketmate/README.md](ticketmate/README.md) | The app: what it does, features in detail, layout |
| [ticketmate/ARCHITECTURE.md](ticketmate/ARCHITECTURE.md) | Design: pipeline, components, security, decisions, deployment, tests |
| [ticketmate/backend/README.md](ticketmate/backend/README.md) | Backend configuration, API, running it by hand |
| [ticketmate/docs/EXTENSION.md](ticketmate/docs/EXTENSION.md) | The extension and the OOD apps; changing and shipping them |
| [ticketmate/VLLM_SETUP.md](ticketmate/VLLM_SETUP.md) | Models, GPUs, the CUDA install trap |
| [ticketmate/tests/README.md](ticketmate/tests/README.md) | All tests; the GPU inference test |
| [rc-kg/README.md](rc-kg/README.md) | The knowledge graph: build, serve, evaluate, variants |
| [rc-kg/API_CONTRACT.md](rc-kg/API_CONTRACT.md) | The HTTP contract between the two |
