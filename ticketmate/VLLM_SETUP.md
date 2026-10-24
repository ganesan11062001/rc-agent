# Model serving — vLLM on Explorer

*Part of [RC Copilot](README.md). This page covers only the model layer: which
GLM model fits Northeastern's A100 hardware and how vLLM is installed and
launched. For what the project does, start with the [README](README.md); for how
the pieces fit together, see [ARCHITECTURE.md](ARCHITECTURE.md).*

vLLM 0.29.0 serving GLM models on a single A100 node.

## Quick start

To get a model *and* the backend in one allocation, use `serve_stack.sbatch` —
it picks the GPU flags up from the command line, so nothing needs editing:

```bash
sbatch --gres=gpu:a100:1 --mem=80G --cpus-per-task=12 \
       --export=ALL,MODEL=qwen7b serve_stack.sbatch
```

See the comment block at the top of that file for the per-model allocations
(`qwen7b`, `qwen14b`, `flash`, `t4-7b`, `t4-14b`, `small`). The OOD app's
form defaults to `t4-7b`, which fits any usable GPU.

For the model *alone* — which is what the rest of this page is about — grab a
GPU node interactively:

```bash
source env.sh
./serve_glm.sh            # GLM-4.7-Flash
MODEL=air ./serve_glm.sh  # GLM-4.5-Air
```

Check it works:

```bash
source env.sh
python smoke_test.py
```

## Files

| File | Purpose |
|---|---|
| `env.sh` | Sourceable env — venv, caches, CUDA paths. Everything else assumes this ran. |
| `serve_glm.sh` | Starts `vllm serve` with the right flags per model. |
| `serve_stack.sbatch` | Slurm job running `serve_glm.sh` *and* the backend together. |
| `smoke_test.py` | Hits `/v1/models` and `/v1/chat/completions` against a running server. |

## Why not GLM-5.3

GLM-5.3 was the original goal. It does not fit on one A100 node, and the
blocker is not just capacity — it is the GPU generation.

GLM-5.3 is a 743B-parameter MoE (39B active) with DeepSeek-style sparse
attention (`GlmMoeDsaForCausalLM`) and a 1M-token context. Per the
[official vLLM recipe](https://recipes.vllm.ai/zai-org/GLM-5.3):

| Variant | Weights | VRAM floor | Hardware requirement |
|---|---|---|---|
| `zai-org/GLM-5.3` (native FP8) | 756 GB | 893 GB | 8×H200 / H20 |
| `gpustack/GLM-5.3-W4A8` | 372 GB | 447 GB | **H100/H200 only** — CUTLASS W4A8 is sm90-only |
| `Inferact/GLM-5.3-NVFP4` | ~465 GB | 558 GB | **Blackwell only** (B200/B300) |
| `zai-org/GLM-5.3-Flash` (FP8) | 328 GB | 386 GB | 8×H100+, nightly docker image only |
| `RedHatAI/GLM-5.3-Flash-NVFP4` | — | 229 GB | **Blackwell only** |
| `zai-org/GLM-5.3-BF16` | 1.5 TB | 1786 GB | multi-node |

One A100 node tops out at 8 × 80 GB = **640 GB**, so on capacity alone only the
two smallest variants are even in range — and both of those are gated on
hardware the A100 does not have:

- A100 is **sm80**. It has no native FP8 tensor cores (those arrive with sm89/sm90),
  so every FP8 checkpoint above is off the table.
- The W4A8 kernel that makes the 447 GB variant possible is explicitly sm90-only.
- NVFP4 requires Blackwell (sm100).

Running GLM-5.3 would need at minimum **8×H200** for the native FP8 checkpoint,
or 8×H100 for the W4A8 variant.

## What fits instead

Every general-access GPU partition on Explorer allows **1 GPU per job**, so the
practical choices are the single-GPU ones. All are BF16 — no FP8 hardware
needed, native on sm80 — except the `t4-*` presets, which are 4-bit AWQ so they
fit a 16 GB T4 (sm75).

| `MODEL=` | Model | Weights | GPUs | Card | Context | On disk |
|---|---|---|---|---|---|---|
| `qwen7b` | Qwen2.5-7B-Instruct | 15.2 GB | **1** | A100-40GB | 32K | ✅ |
| `qwen14b` | Qwen2.5-14B-Instruct | 29.6 GB | **1** | A100-80GB | 32K | ✗ |
| `small` | Qwen2.5-3B-Instruct | 6 GB | **1** | any sm75+ | 16K | ✅ |
| `t4-7b` | Qwen2.5-7B-Instruct-AWQ (4-bit) | 5.5 GB | **1** | T4 or better | 16K | ✅ |
| `t4-14b` | Qwen2.5-14B-Instruct-AWQ (4-bit) | 9.5 GB | **1** | T4 or better | 8K | ✗ |
| `flash` | GLM-4.7-Flash | 59 GB | 1 | A100-80GB | ≤32K † | ✅ |
| `flash` | GLM-4.7-Flash | 59 GB | 2 | A100-80GB | 131K | ✅ |
| `air` | GLM-4.5-Air | 221 GB | 4 | A100-80GB | 131K | ✗ |

† On a single 80GB card, 59 GB of weights leaves only ~9 GB for KV. Pass
`MAX_LEN=32768` or the engine fails to allocate a usable KV cache. The 2-GPU
TP=2 run is the verified one (41.2 GiB KV, 817K tokens, 6.24x concurrency).

**Recommended single-GPU default: `qwen7b`.** It is the smallest allocation that
is still a real model, needs only a 40GB A100 (a much shorter queue than 80GB),
and runs TP=1 so there is no NCCL setup to go wrong.

GLM-4.7-Flash remains the strongest option if you can get 2 GPUs — it is a ~30B
MoE, and it uses the same `glm47` tool-call and `glm45` reasoning parsers as
GLM-5.3, so client code ports to GLM-5.3 unchanged given Hopper access later.

## V100 is not usable

Do not request V100 nodes, however idle they look.

`torch 2.13.0+cu129` ships kernels for `sm_75, sm_80, sm_86, sm_90, sm_100,
sm_120`. V100 is **sm_70**, which upstream PyTorch dropped; a V100 job dies with
`no kernel image is available for execution on the device` before dtype or
memory is ever considered. Volta also has no bfloat16 and no FlashAttention.

Supporting it would mean a second legacy venv — torch ≤2.6 on cu124 plus vLLM
0.6.x, fp16, xformers backend — and vLLM 0.6.x predates GLM-4.5/4.7 support
entirely. Not worth maintaining.

**T4 (sm75) does work**, via the automatic fp16 fallback in `serve_glm.sh`, but
only has 16 GB — enough for `small`, not for `qwen7b`.

## Environment notes

- Everything (venv, uv, HF cache, Triton cache) lives under `/projects/rc/projects/ticketmate` (venv in `ticketmate/.venv`; uv, HF and compile caches shared at the top level)
  to stay off the home quota. `env.sh` sets `HF_HOME` accordingly.
- torch is pinned to the **cu129** build. vLLM 0.29.0 requires torch 2.13.0,
  which is published only on the cu129 and cu130 indexes. cu129 is the safer
  pick for an older A100 driver.
- Do not install with `--torch-backend=auto` from a login/VNC node: with no GPU
  present it silently resolves to `torch==2.13.0+cpu`, which cannot serve.
  Always pass `--torch-backend=cu129` explicitly.
- The module system is broken on the node this was set up from
  (`modulecmd.tcl` missing), which is why the install uses a self-contained
  `uv` + standalone CPython 3.12 rather than `module load anaconda3`.

## Verifying on a GPU node

The install was done on a CPU-only node, so the CUDA path is unverified. On the
A100 node run:

```bash
source env.sh
nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.device_count())"
```

Expect `2.13.0+cu129` and a nonzero device count. Set `TP` in `serve_glm.sh` to
match the GPU count.
