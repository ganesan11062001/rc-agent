#!/usr/bin/env bash
# Source this before running anything: `source env.sh`
# Sets up the vLLM venv + caches on /projects (home dirs have tight quotas).

# Overridable, like every other TM_ROOT in the tree; by default the checkout
# this file is in, so a checkout anywhere works without editing it.
export TM_ROOT="${TM_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
# The monorepo root: the uv toolchain and caches there are shared with rc-kg.
export TICKETMATE_HOME="${TICKETMATE_HOME:-$(cd "$TM_ROOT/.." && pwd)}"

# uv + the project venv
export PATH="$TICKETMATE_HOME/.uv/bin:$PATH"
export UV_CACHE_DIR="$TICKETMATE_HOME/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$TICKETMATE_HOME/.uv/python"
export VIRTUAL_ENV="$TM_ROOT/.venv"
export PATH="$VIRTUAL_ENV/bin:$PATH"

# Keep the multi-hundred-GB model downloads off $HOME
export HF_HOME="$TICKETMATE_HOME/.cache/huggingface"
export HF_HUB_ENABLE_HF_TRANSFER=1

# Never contact huggingface.co from a compute node. Those nodes reach the
# internet only through a site proxy, and the Hub call is flaky there: a
# Qwen2.5-7B-AWQ launch logged "Could not reach the Hub ([Errno 99] Cannot
# assign requested address)" and then hung for twelve minutes without ever
# loading, despite the weights being cached locally.
#
# With this set, a cached model loads straight from disk and an uncached one
# fails immediately with a clear message instead of hanging. Download new
# models from a LOGIN node first:
#
#   source cluster/env.sh
#   HF_HUB_OFFLINE=0 python -c "from huggingface_hub import snapshot_download; \
#       snapshot_download('Qwen/Qwen2.5-7B-Instruct-AWQ')"
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"

# vLLM scratch: compiled kernels and grammar caches, written on every launch.
# The shared copies under $TICKETMATE_HOME/.cache belong to whoever installed the
# project, so other RC members cannot write them; they get their own under
# /scratch instead (regenerable -- a purge only costs one slower start).
# The model weights in $HF_HOME are only read, so those stay shared.
_tm_cache() {
  if [ -w "$TICKETMATE_HOME/.cache/$1" ]; then echo "$TICKETMATE_HOME/.cache/$1"
  else echo "/scratch/$USER/rc_copilot_cache/$1"; fi
}
export VLLM_CACHE_ROOT="${VLLM_CACHE_ROOT:-$(_tm_cache vllm)}"
export OUTLINES_CACHE_DIR="${OUTLINES_CACHE_DIR:-$(_tm_cache outlines)}"
export TRITON_CACHE_DIR="${TRITON_CACHE_DIR:-$(_tm_cache triton)}"
[ -w "$UV_CACHE_DIR" ] || export UV_CACHE_DIR="/scratch/$USER/rc_copilot_cache/uv"

mkdir -p "$VLLM_CACHE_ROOT" "$TRITON_CACHE_DIR" "$OUTLINES_CACHE_DIR"
[ -d "$HF_HOME" ] || mkdir -p "$HF_HOME"

# CUDA runtime from the cluster module tree (A100 = sm80).
# The torch wheels bundle their own CUDA libs, so this is only needed for nvcc
# and for tools that link against the system CUDA.
if [ -d /shared/EL9/explorer/cuda/12.8.0 ]; then
  export CUDA_HOME=/shared/EL9/explorer/cuda/12.8.0
  export PATH="$CUDA_HOME/bin:$PATH"
  export LD_LIBRARY_PATH="$CUDA_HOME/lib64:${LD_LIBRARY_PATH:-}"
fi

# RC's playbooks (internal procedures) are kept outside the repository.
if [ -z "${PLAYBOOK_DIR:-}" ] && [ -d /projects/rc/projects/rc-playbooks ]; then
  export PLAYBOOK_DIR=/projects/rc/projects/rc-playbooks
fi
