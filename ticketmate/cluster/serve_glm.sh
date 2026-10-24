#!/usr/bin/env bash
# Launch a vLLM OpenAI-compatible server for a GLM model on one A100 node.
#
#   source env.sh
#   ./serve_glm.sh                 # defaults to GLM-4.7-Flash
#   MODEL=air ./serve_glm.sh       # GLM-4.5-Air  (needs 4x A100-80GB)
#   TP=4 ./serve_glm.sh            # override tensor parallelism
#
# NOTE: GLM-5.3 is NOT servable here -- see README.md. A100 is sm80 and has no
# native FP8/W4A8 kernels; the smallest GLM-5.3 variant needs 447 GB on Hopper.

set -euo pipefail

if [ -z "${VIRTUAL_ENV:-}" ]; then
  echo "error: run 'source env.sh' first" >&2
  exit 1
fi

MODEL="${MODEL:-flash}"
PORT="${PORT:-8000}"

case "$MODEL" in
  flash)
    # ~30B MoE (64 experts, 4 active), 62.5 GB bf16, 198K context.
    MODEL_ID="zai-org/GLM-4.7-Flash"
    SERVED_NAME="glm-4.7-flash"
    DEFAULT_TP=2
    MAX_LEN="${MAX_LEN:-131072}"
    ;;
  air)
    # 106B total / 12B active, 221 GB bf16, 131K context.
    MODEL_ID="zai-org/GLM-4.5-Air"
    SERVED_NAME="glm-4.5-air"
    DEFAULT_TP=4
    MAX_LEN="${MAX_LEN:-131072}"
    ;;
  qwen7b)
    # SINGLE-GPU production option. 15.2 GB bf16, 32K context, GQA with only 4
    # KV heads (~56 KB/token), so KV cache is cheap. Fits one A100-40GB with
    # ~20 GB left for KV -- no tensor parallelism, no NCCL, shorter queue than
    # the 2x80GB that GLM-4.7-Flash needs.
    MODEL_ID="Qwen/Qwen2.5-7B-Instruct"
    SERVED_NAME="qwen2.5-7b-instruct"
    DEFAULT_TP=1
    MAX_LEN="${MAX_LEN:-32768}"
    GLM_PARSERS=0
    ;;
  qwen14b)
    # Single-GPU, but needs the 80GB A100 (29.6 GB weights, ~42 GB KV left).
    # Stronger than 7B; use --constraint=a100@80g or it may land on a 40GB card.
    MODEL_ID="Qwen/Qwen2.5-14B-Instruct"
    SERVED_NAME="qwen2.5-14b-instruct"
    DEFAULT_TP=1
    MAX_LEN="${MAX_LEN:-32768}"
    GLM_PARSERS=0
    ;;
    t4-7b)
      # T4 DEPLOYMENT OPTION. 4-bit AWQ, ~5.5 GB of weights, so it fits a 16 GB
      # T4 with roughly 8 GB left for KV cache. vLLM permits AWQ from sm_75,
      # which is exactly what a T4 is. Substantially stronger than the 3B at
      # fp16, and it still leaves headroom.
      MODEL_ID="Qwen/Qwen2.5-7B-Instruct-AWQ"
      SERVED_NAME="qwen2.5-7b-instruct-awq"
      DEFAULT_TP=1
      MAX_LEN="${MAX_LEN:-16384}"
      GLM_PARSERS=0
      ;;
    t4-14b)
      # Also T4-capable, but tight: ~9.5 GB of 4-bit weights leaves ~4 GB for
      # KV, roughly 20k tokens. Shorter context, less concurrency.
      MODEL_ID="Qwen/Qwen2.5-14B-Instruct-AWQ"
      SERVED_NAME="qwen2.5-14b-instruct-awq"
      DEFAULT_TP=1
      MAX_LEN="${MAX_LEN:-8192}"
      GLM_PARSERS=0
      ;;
  small)
    # Not for production drafting -- this exists so the end-to-end pipeline and
    # the prompt can be exercised without queueing for an A100. ~6 GB in bf16,
    # so it lands on whatever GPU is free, including a 40GB A100 or smaller.
    MODEL_ID="Qwen/Qwen2.5-3B-Instruct"
    SERVED_NAME="qwen2.5-3b-instruct"
    DEFAULT_TP=1
    MAX_LEN="${MAX_LEN:-16384}"
    GLM_PARSERS=0
    ;;
  *)
    echo "error: MODEL must be one of flash, air, qwen7b, qwen14b," \
           "t4-7b, t4-14b, small (got '$MODEL')" >&2
    exit 1
    ;;
esac

TP="${TP:-$DEFAULT_TP}"
GLM_PARSERS="${GLM_PARSERS:-1}"

echo "Serving $MODEL_ID  (TP=$TP, max_model_len=$MAX_LEN, port=$PORT)"

# A100 (sm80) notes:
#   - bf16 weights, bf16 KV cache. Do NOT pass --kv-cache-dtype fp8; sm80 has no
#     native FP8 and the emulated path is slower and less accurate.
#   - MTP speculative decoding is left off; enable it once base serving is
#     confirmed working, via:
#       --speculative-config '{"method":"mtp","num_speculative_tokens":3}'
# ── dtype: decided by the GPU, not the model ─────────────────────────────────
# Explorer has T4 (sm75) and V100 (sm70) alongside A100 (sm80) and H200 (sm90).
# bfloat16 needs sm80+. Most model configs (Qwen2.5 included) ask for bfloat16,
# so on a T4 vLLM aborts at startup with:
#   "Bfloat16 is only supported on GPUs with compute capability of at least 8.0"
# Those older cards are usually the idle ones, so detect and fall back to fp16.
#
# V100 IS A DEAD END -- do not request one. The fallback below cannot rescue it.
# torch 2.13.0+cu129 ships kernels for sm_75/80/86/90/100/120 only; sm_70 was
# dropped upstream. A V100 fails with "no kernel image is available for
# execution on the device" before dtype is ever consulted. Serving on V100 would
# need a separate legacy venv (torch <=2.6 cu124 + vLLM 0.6.x, fp16, xformers),
# which predates GLM-4.5/4.7 support entirely. T4 (sm75) does work via fp16.
if [ -z "${DTYPE:-}" ]; then
  # `|| true` matters: this script runs under `set -euo pipefail`, so without it
  # a missing or unhappy nvidia-smi kills the server here with no message at all.
  CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || true)"
  case "$CC" in
    "")        DTYPE="auto"     ;;   # no GPU visible yet; let vLLM decide
    7.*|6.*|5.*) DTYPE="float16" ;;   # pre-Ampere: no bfloat16
    *)         DTYPE="auto"     ;;   # sm80+ : use the model's own dtype
  esac
  [ "$DTYPE" = "float16" ] && \
    echo "note: compute capability $CC has no bfloat16 -> using --dtype float16"
fi

ARGS=(
  --served-model-name "$SERVED_NAME"
  --tensor-parallel-size "$TP"
  --max-model-len "$MAX_LEN"
  --gpu-memory-utilization 0.90
  --dtype "$DTYPE"
  --host "${VLLM_HOST:-0.0.0.0}"
  --port "$PORT"
)

# GLM-specific parsers. Passing these to a non-GLM model makes vLLM exit at
# startup, so they are opt-out for the small test model.
if [ "$GLM_PARSERS" = "1" ]; then
  ARGS+=(--tool-call-parser glm47 --reasoning-parser glm45 --enable-auto-tool-choice)
fi

exec vllm serve "$MODEL_ID" "${ARGS[@]}"
