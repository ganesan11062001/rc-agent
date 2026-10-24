# TicketMate

Drafts replies to Research Computing ServiceNow tickets with a model served
on the cluster, grounded in the RC website and rc-docs.

```
/projects/rc/projects/ticketmate/     this repository
├── ticketmate/    the app: FastAPI backend, Chrome extension, OOD app, Slurm scripts
├── rc-kg/         the knowledge graph: Neo4j snapshot builds and the retrieval API
├── rc-copilot/    published OOD releases (not in git; written by publish.sh)
├── .uv/           shared Python toolchain (not in git)
└── .cache/        model weights and compile caches (not in git)
```

## Running it

### Using it (RC team members)

```bash
/projects/rc/projects/ticketmate/rc-copilot/install.sh     # once: links the OOD app into ~/ondemand/dev
```

1. **Open OnDemand → Develop → My Sandbox Apps → RC Copilot → Launch.** The
   defaults (Qwen2.5-7B AWQ on any free GPU) schedule in seconds.
2. **Install the extension once.** Unzip `rc-copilot/rc-copilot-extension.zip`,
   then in Chrome go to `chrome://extensions`, turn on Developer mode, click
   **Load unpacked** and select the folder.
3. **Open My Interactive Sessions** in OOD. The extension picks up the session
   address and token on its own.
4. **Open a ticket in ServiceNow and click Draft reply.** The draft is written
   into work notes unsaved, for you to check, edit and save.

The model takes a few minutes to load and the knowledge graph 1–2 minutes;
the panel shows progress meanwhile. More in
[ticketmate/ood-apps/MEMBERS.md](ticketmate/ood-apps/MEMBERS.md).

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
# needs network to rc.northeastern.edu, so run it from a login node)
rc-kg/scripts/install_neo4j.sh
python3 -m venv rc-kg/.venv
rc-kg/.venv/bin/pip install -r rc-kg/requirements.txt
rc-kg/scripts/build_graph.sh --all

# Release the OOD app to rc-copilot/ -- this changes everyone's app at once
ticketmate/ood-apps/publish.sh
```

Model weights are cached in `.cache/huggingface` the first time a model is
used. To keep the graph current, run `rc-kg/scripts/rebuild.sbatch` weekly from
`scrontab` (instructions are in its header). `publish.sh --list` and
`publish.sh --rollback` manage releases.

**After moving or renaming the checkout, republish.** OOD runs a copy of the
session script, and the only place that copy learns the checkout's location is
the `ticketmate.env` that `publish.sh` writes beside it.

### Without OOD

| Goal | Command | Details |
| --- | --- | --- |
| Model + backend + graph in one batch job | `sbatch --export=ALL,MODEL=t4-7b --gres=gpu:t4:1 ticketmate/cluster/serve_stack.sbatch` | [EXTENSION.md](ticketmate/docs/EXTENSION.md) |
| Tune the prompt in the web page, against any vLLM endpoint | `cd ticketmate/backend && .venv/bin/uvicorn app.main:app --port 8080` | [backend/README.md](ticketmate/backend/README.md) |
| Check inference works on a GPU | `sbatch --gres=gpu:t4:1 ticketmate/tests/run_inference_test.sbatch` | [tests/README.md](ticketmate/tests/README.md) |
| Serve the graph on its own | `rc-kg/scripts/serve_kg.sh --port 8090` | [rc-kg/README.md](rc-kg/README.md) |

### Tests

```bash
ticketmate/tests/rc_kg/run_contract.sh      # TicketMate against a real rc-kg, no GPU, ~2 min
rc-kg/scripts/contract_test.sh              # rc-kg's side of the API contract
(cd rc-kg && .venv/bin/python -m pytest tests -q)      # rc-kg unit tests
ticketmate/tests/startup/run_slow_start.sh  # startup progress display, no GPU
ticketmate/tests/ui/run.sh                  # extension and web UI, headless browser
```

## How the pieces connect

The two services talk only over rc-kg's versioned HTTP API
([rc-kg/API_CONTRACT.md](rc-kg/API_CONTRACT.md)); a change to it bumps the
version and runs both contract tests:

```bash
rc-kg/scripts/contract_test.sh             # provider side
ticketmate/tests/rc_kg/run_contract.sh     # consumer side
```

No script hard-codes this location: each finds the repository from its own
path, and the OOD app is told it when published. To move the checkout, move
the directory and republish (`ticketmate/ood-apps/publish.sh`).

- App: [ticketmate/README.md](ticketmate/README.md)
- Knowledge graph: [rc-kg/README.md](rc-kg/README.md)
- For RC members: [ticketmate/ood-apps/MEMBERS.md](ticketmate/ood-apps/MEMBERS.md)
