#!/usr/bin/env bash
# Reproducible vLLM install for Explorer (A100, driver 570.x).
#
#   ./install_vllm.sh --check     report what's installed, change nothing
#   ./install_vllm.sh             install / repair
#
# WHY THIS SCRIPT EXISTS
#
# `pip install vllm` gives you the wrong build on this cluster. As of 0.29.0 the
# default PyPI wheel is compiled against CUDA 13, which needs a 580-series
# driver. Explorer's A100 nodes run 570.86.15. The failure is a two-stage trap:
#
#   1. ImportError: libcudart.so.13: cannot open shared object file
#   2. after you put libcudart.so.13 on the path, vLLM's own CUDA kernels then
#      die at runtime with "CUDA driver version is insufficient for CUDA
#      runtime version" (cudaErrorInsufficientDriver) -- torch still works,
#      because CUDA 12.x has minor-version compatibility, so the model loads
#      and only fails once a vLLM kernel is called.
#
# The fix is the explicitly-tagged +cu129 wheel from the GitHub release. Note
# that `uv pip install vllm --torch-backend=cu129` is NOT enough: that flag
# only selects the torch wheel, not vLLM's own compiled extension.

set -uo pipefail

TM_ROOT="${TM_ROOT:-/projects/rc/projects/ticket_mate}"
VLLM_VERSION="${VLLM_VERSION:-0.29.0}"
CUDA_TAG="${CUDA_TAG:-cu129}"
PY_VERSION="${PY_VERSION:-3.12}"

VENV="$TM_ROOT/.venv"
UV="$TM_ROOT/.uv/bin/uv"
EXT_GLOB="$VENV/lib/python${PY_VERSION}/site-packages/vllm/_C_stable_libtorch*.so"

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32mOK\033[0m    %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
info() { printf '  ---   %s\n' "$*"; }

# ── 0. driver check: decides which wheel is correct ──────────────────────────
say "== driver =="
if command -v nvidia-smi >/dev/null 2>&1; then
  DRIVER="$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)"
  MAJOR="${DRIVER%%.*}"
  info "driver $DRIVER"
  if [ -n "$MAJOR" ] && [ "$MAJOR" -ge 580 ] 2>/dev/null; then
    info "580+ can run CUDA 13; the default PyPI wheel would also work."
  else
    info "below 580 -> CUDA 13 is NOT usable. Must use the +${CUDA_TAG} wheel."
  fi
else
  info "no nvidia-smi here (login/CPU node). Assuming driver 570.x -> ${CUDA_TAG}."
fi

# ── 1. report ────────────────────────────────────────────────────────────────
report() {
  say ""
  say "== installed =="
  if [ ! -x "$VENV/bin/python" ]; then bad "no venv at $VENV"; return 1; fi

  "$VENV/bin/python" - <<'PY'
import importlib.metadata as m
for p in ("vllm", "torch"):
    try: print("  ---   %-8s %s" % (p, m.version(p)))
    except Exception: print("  ---   %-8s (not installed)" % p)
PY

  local so
  so="$(ls $EXT_GLOB 2>/dev/null | head -1)"
  if [ -z "$so" ]; then bad "vLLM CUDA extension not found"; return 1; fi

  local needs
  needs="$(ldd "$so" 2>/dev/null | grep -o 'libcudart\.so\.[0-9]*' | sort -u | tr '\n' ' ')"
  case "$needs" in
    *libcudart.so.12*) ok "extension links libcudart.so.12 (correct for driver 570.x)" ;;
    *libcudart.so.13*) bad "extension links libcudart.so.13 -- WRONG build, will fail at runtime" ; return 1 ;;
    *)                 bad "could not determine CUDA linkage ($needs)" ; return 1 ;;
  esac

  if "$VENV/bin/python" -c "import vllm" >/dev/null 2>&1; then
    ok "import vllm succeeds"
  else
    # libcuda.so.1 only exists on GPU nodes; that miss is expected elsewhere.
    if "$VENV/bin/python" -c "import vllm" 2>&1 | grep -q 'libcuda\.so\.1'; then
      ok "import reaches the driver library (run on a GPU node to load fully)"
    else
      bad "import vllm fails:"
      "$VENV/bin/python" -c "import vllm" 2>&1 | tail -3 | sed 's/^/        /'
      return 1
    fi
  fi
  return 0
}

if [ "$CHECK_ONLY" = 1 ]; then
  report && { say ""; say "Nothing to do -- the install is correct."; exit 0; }
  say ""; say "Re-run without --check to repair."; exit 1
fi

report && { say ""; say "Already correct. Re-installing anyway would be a no-op; exiting."; exit 0; }

# ── 2. environment ───────────────────────────────────────────────────────────
say ""
say "== installing =="
# Caches go on /projects: home directories have tight quotas and the model
# downloads and wheels are tens of GB.
export UV_CACHE_DIR="$TM_ROOT/.cache/uv"
export UV_PYTHON_INSTALL_DIR="$TM_ROOT/.uv/python"
mkdir -p "$UV_CACHE_DIR" "$UV_PYTHON_INSTALL_DIR"

if [ ! -x "$UV" ]; then
  info "installing uv into $TM_ROOT/.uv"
  curl -LsSf https://astral.sh/uv/install.sh | \
    UV_INSTALL_DIR="$TM_ROOT/.uv/bin" sh || { bad "uv install failed"; exit 1; }
fi
info "uv $("$UV" --version 2>/dev/null)"

if [ ! -x "$VENV/bin/python" ]; then
  info "creating venv (python $PY_VERSION)"
  "$UV" venv --python "$PY_VERSION" "$VENV" || { bad "venv creation failed"; exit 1; }
fi

# Stale bytecode blocks directory removal on NFS ("Directory not empty",
# errno 39) when uv replaces the package. Clear it first.
if [ -d "$VENV/lib/python${PY_VERSION}/site-packages/vllm" ]; then
  info "clearing stale __pycache__ (NFS blocks removal otherwise)"
  find "$VENV/lib/python${PY_VERSION}/site-packages/vllm" \
    -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null
fi

WHEEL="https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}+${CUDA_TAG}-cp38-abi3-manylinux_2_28_$(uname -m).whl"
info "wheel: $WHEEL"

"$UV" pip install --python "$VENV/bin/python" "$WHEEL" \
  --extra-index-url "https://download.pytorch.org/whl/${CUDA_TAG}" \
  || { bad "install failed"; exit 1; }

# ── 3. verify ────────────────────────────────────────────────────────────────
say ""
if report; then
  say ""
  say "Done. Next:"
  say "    source $TM_ROOT/cluster/env.sh"
  say "    sbatch cluster/serve_stack.sbatch"
else
  say ""
  bad "install completed but verification failed -- see above"
  exit 1
fi
