---
name: 2. Repository and environment
route: /repository
menu: Foundations
---

# 2. Repository and environment

## 2.1 Layout

```
<repo>/                                  /projects/rc/projects/ticketmate  (or rc-agent)
├── README.md
├── docs/developer/                      this guide
├── ticketmate/                          the application
│   ├── backend/
│   │   ├── app/                         FastAPI service (chapter 4)
│   │   │   ├── main.py                  routes, the draft pipeline
│   │   │   ├── ticket_context.py        cleaning, query, fitting (5)
│   │   │   ├── lookups.py               read-only cluster lookups (6)
│   │   │   ├── playbooks.py, playbooks/ RC procedures as Markdown files (7)
│   │   │   ├── prompts.py               every prompt string (8)
│   │   │   ├── llm_client.py            the vLLM client (8)
│   │   │   ├── kg_client.py             rc-kg client: retrieve, cite, similarity (9, 10)
│   │   │   ├── models.py                Pydantic request/response models, the draft JSON schema
│   │   │   ├── evaluators.py            assertions, metrics, background LLM judge (10)
│   │   │   ├── runlog.py, tracing.py    run logs and run trees (10)
│   │   │   ├── auth.py                  access token and signed sessions (11)
│   │   │   ├── startup.py               startup progress for /api/health
│   │   │   ├── config.py                settings (14)
│   │   │   └── agents/, guardrails.py, context_pack.py   rc-agent only (15-16)
│   │   ├── web/                         paste-a-ticket page (no build step)
│   │   ├── .venv/                       Python 3.12 environment
│   │   └── requirements.txt
│   ├── extension/                       Chrome MV3 extension (3)
│   ├── ood-apps/rc-copilot/             the Open OnDemand app (3)
│   ├── ood-apps/{publish,install}.sh    release the app / link a member's sandbox
│   ├── cluster/                         sbatch launchers, vLLM install and launch, diagnostics
│   ├── tests/                           (12)
│   ├── ops/                             ops report over the trace store (10, 17)
│   ├── evals/                           rc-agent only (17)
│   └── ARCHITECTURE.md, VLLM_SETUP.md, docs/EXTENSION.md
├── rc-kg/                               the knowledge graph (9)
│   ├── ingest/  enrich/  graph/         build: crawl, section, embed, tag topics, write Neo4j
│   ├── retrieval/  api/                 serve: hybrid retrieval, citations, support, HTTP API
│   ├── eval/                            retrieval and citation evaluation
│   ├── scripts/                         build, serve, instance management, Neo4j install
│   ├── topics.yaml                      the closed topic vocabulary
│   ├── data/graph/, data/graph-website/ published snapshots (git-ignored)
│   ├── vendor/                          Neo4j, the embedding model, a runtime archive (git-ignored)
│   └── .venv/                           Python 3.12 environment
├── .uv/  .cache/                        shared uv toolchain and caches, model weights (git-ignored)
└── traces/$USER/                        every run log, span and metric; rc-agent adds guardrails and evals (10, 17)
```

## 2.2 Python environments

Two separate environments, because their dependencies differ a lot:

| environment | path | main dependencies |
|---|---|---|
| backend | `ticketmate/backend/.venv` | fastapi, uvicorn, openai, pydantic(-settings), opentelemetry; rc-agent adds langgraph, pyyaml |
| rc-kg | `rc-kg/.venv` | neo4j driver, sentence-transformers (torch), beautifulsoup4, requests, fastapi |
| vLLM | under `.uv`, made by `cluster/install_vllm.sh` | vLLM `+cu129` (see `VLLM_SETUP.md`: Explorer's driver needs the CUDA 12.9 build) |

They are created with `uv` from the shared toolchain in `.uv/`:

```bash
.uv/bin/uv venv --python 3.12 ticketmate/backend/.venv
.uv/bin/uv pip install --python ticketmate/backend/.venv/bin/python -r ticketmate/backend/requirements.txt
```

`cluster/env.sh` sets the environment the launchers use: cache locations
under the repository (`.cache/`), `HF_HOME`, the vLLM install, proxy
bypasses (`no_proxy` -- Explorer's nodes set `http_proxy`, which refuses
internal traffic).

## 2.3 Running it

### Through Open OnDemand (how RC members use it)

1. Install once: `/projects/rc/projects/<repo>/rc-copilot/install.sh` links
   `~/ondemand/dev/rc-copilot` (rc-agent: `rc-agent`) to the published release.
2. Launch *RC Copilot* (rc-agent: *RC Agent*) from OOD's interactive apps:
   pick the model and hours.
3. `script.sh.erb` in the job: starts rc-kg (`scripts/serve_kg.sh`: loads the
   snapshot into a private Neo4j on node-local disk), then the backend
   immediately (so the extension can show progress), then vLLM.
4. Open *My Interactive Sessions*: `ood-connect.js` reads the session card and
   configures the extension. Click **Draft reply** on a ticket.

The session runs the code straight from the checkout: a backend or rc-kg
change is live in the next session without republishing. Only the OOD
templates and the extension zip need `ood-apps/publish.sh`.

### As a batch job

```bash
sbatch --gres=gpu:a100:1 --mem=80G --cpus-per-task=12 \
       --export=ALL,MODEL=qwen7b ticketmate/cluster/serve_stack.sbatch
tail -f ticketmate/logs/rc-copilot-<jobid>.out      # prints the URL; the token is in ~/.rc_copilot/token
```

Paste URL and token into the extension's popup.

### For development, without a GPU

The backend runs without a model; draft calls then fail cleanly. For real
work against a stand-in model, use the stub the tests use:

```bash
python3 ticketmate/tests/rc_kg/stub_vllm.py 8001 &                     # an OpenAI-compatible stub
rc-kg/scripts/serve_kg.sh --port 8002 &                                # a private rc-kg on the published snapshot
cd ticketmate/backend
VLLM_BASE_URL=http://127.0.0.1:8001/v1 RC_KG_URL=http://127.0.0.1:8002 \
  RC_KG_API_TOKEN=$(cat ...) AUTH_DISABLED=true .venv/bin/uvicorn app.main:app --port 8080
```

Or call the pipeline in-process, which is what the evals do:

```python
import asyncio, sys; sys.path.insert(0, "ticketmate/backend")
from app import main
from app.models import DraftRequest
r = asyncio.run(main.draft(DraftRequest(ticket_text="My job 4812345 was killed, OUT_OF_MEMORY")))
print(r.draft_response)
```

## 2.4 Models

`cluster/serve_glm.sh` maps a `MODEL` name to a model, served name, dtype and
context length; `serve_stack.sbatch` and the OOD form pass it.

| MODEL | model | fits | context |
|---|---|---|---|
| `t4-7b` (OOD default) | Qwen2.5-7B-Instruct-AWQ, 4-bit | a 16 GB T4 | 16,384 |
| `t4-14b` | Qwen2.5-14B-Instruct-AWQ, 4-bit | T4, tight | 8,192 |
| `small` | Qwen2.5-3B-Instruct | any GPU (smoke tests only) | 16,384 |
| `qwen7b` | Qwen2.5-7B-Instruct, fp16/bf16 | A100 40 GB | 32,768 |
| `qwen14b` | Qwen2.5-14B-Instruct | A100 80 GB | 32,768 |
| `flash` | GLM-4.7-Flash (30B MoE) | 2 x A100 80 GB / H200 | 131,072 |
| `air` | GLM-4.5-Air (106B MoE) | 4 GPUs (multigpu approval) | 131,072 |

V100s (sm_70) cannot run it (no kernels in torch 2.13+cu129); T4s have no
bfloat16, so `serve_glm.sh` falls back to float16.

> **rc-agent:** Qwen models are started with `--enable-auto-tool-choice
> --tool-call-parser hermes`, which the agents need (`QWEN_TOOLS=0` turns it off).
