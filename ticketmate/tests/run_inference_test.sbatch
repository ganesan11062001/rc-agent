#!/usr/bin/env bash
#SBATCH --job-name=rc-inftest
#SBATCH --output=logs/inftest-%j.out
#SBATCH --error=logs/inftest-%j.err
#SBATCH --partition=gpu-interactive
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=8
#SBATCH --mem=32G
#SBATCH --time=01:00:00
#
# Self-contained answer to one question: can this cluster run inference, and
# does the RC Copilot prompt produce a usable draft?
#
#   cd /projects/rc/projects/ticket_mate/tests
#   sbatch run_inference_test.sbatch
#   tail -f logs/inftest-<jobid>.out
#
# Defaults to the 3B test model on ANY free GPU, so it schedules in seconds.
# Qwen2.5-3B, Qwen2.5-7B and GLM-4.7-Flash are all cached on /projects, so none
# of the commands below download anything.
#
# General-access partitions cap GRES at one GPU per job, so prefer these:
#
#   Qwen2.5-7B on one A100 40GB -- recommended single-GPU check:
#
#     sbatch --gres=gpu:a100:1 --mem=80G --cpus-per-task=12 \
#            --export=ALL,MODEL=qwen7b run_inference_test.sbatch
#
#   GLM-4.7-Flash on one A100 80GB (TP and MAX_LEN are set automatically from
#   the allocation; 59 GB of weights leaves ~12 GB for KV, so context drops to
#   32k):
#
#     sbatch --gres=gpu:a100:1 --constraint=a100@80g --mem=120G \
#            --cpus-per-task=16 --export=ALL,MODEL=flash run_inference_test.sbatch
#
# Do not request V100 -- torch 2.13+cu129 has no sm_70 kernels.
#
# To judge quality on a real ticket, drop it in a file and point at it:
#
#   sbatch --export=ALL,TICKET=/path/to/ticket.txt run_inference_test.sbatch
#
# Touches nothing outside tests/ -- it reads backend/app/prompts.py and
# backend/app/models.py, and writes only into tests/logs/.

set -uo pipefail

TM_ROOT="${TM_ROOT:-/projects/rc/projects/ticket_mate}"
MODEL="${MODEL:-small}"
TICKET="${TICKET:-$TM_ROOT/tests/sample_ticket.txt}"
INSTRUCTIONS="${INSTRUCTIONS:-}"

cd "${SLURM_SUBMIT_DIR:-$TM_ROOT/tests}" || exit 1

# Slurm opened --output=logs/... before this script ran, and does not create
# intermediate directories. If logs/ was missing at submission time there is
# nowhere for this message to go -- which is exactly the confusing case -- so
# keep the directory in the repo rather than relying on this.
mkdir -p logs

echo "======================================================================"
echo " node        $(hostname)"
echo " job         ${SLURM_JOB_ID:-n/a}"
echo " model       $MODEL"
echo " ticket      $TICKET"
echo "======================================================================"

if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total,compute_cap,driver_version --format=csv
else
  echo "ERROR: no nvidia-smi -- this is not a GPU node."
  exit 1
fi

source "$TM_ROOT/cluster/env.sh"

# --- derive TP and context from the GPUs we were actually given -------------
# serve_glm.sh defaults MODEL=flash to TP=2. If Slurm gave us one GPU that
# mismatch only surfaces after the queue wait, so settle it here instead.
GPU_COUNT="$(nvidia-smi --query-gpu=index --format=csv,noheader | wc -l)"
GPU_MIB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)"
TOTAL_MIB=$(( GPU_COUNT * GPU_MIB ))

echo
echo "=== allocation: ${GPU_COUNT} GPU(s), ${GPU_MIB} MiB each (${TOTAL_MIB} MiB total) ==="

export TP="${TP:-$GPU_COUNT}"

# Architecture first: torch 2.13+cu129 ships sm_75/80/86/90/100/120 and no PTX,
# so a V100 (sm_70) has no compiled code at all -- not a dtype problem, and the
# float16 fallback in serve_glm.sh cannot help. Fail in seconds with a clear
# reason rather than an opaque CUDA error several minutes in.
CC="$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ' || true)"
CC_MAJOR="${CC%%.*}"
CC_MINOR="${CC##*.}"
if [ -n "$CC" ] && { [ "${CC_MAJOR:-9}" -lt 7 ] 2>/dev/null || \
     { [ "${CC_MAJOR:-9}" = "7" ] && [ "${CC_MINOR:-5}" -lt 5 ] 2>/dev/null; }; }; then
  GPU_NAME="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
  echo
  echo "ERROR: this is a ${GPU_NAME} (compute capability ${CC})."
  echo "       torch $(python -c 'import torch; print(torch.__version__)' 2>/dev/null) is compiled for sm_75 and above"
  echo "       with no PTX fallback, so there is no code that can run here."
  echo
  echo "       Pick hardware that works:"
  echo "         --gres=gpu:t4:1                        (sm_75, 16 GB, 3B model)"
  echo "         --gres=gpu:a100:1                      (sm_80, 40 GB, up to 7B)"
  echo "         --gres=gpu:a100:1 --constraint=a100@80g (80 GB, GLM-4.7-Flash)"
  exit 1
fi
echo "=== compute capability ${CC:-unknown} -- supported ==="

if [ "$MODEL" = "flash" ] || [ "$MODEL" = "air" ]; then
  # GLM-4.7-Flash is ~56 GiB of weights; Air is far larger. Refuse now rather
  # than spend ten minutes loading into a card that cannot hold it.
  NEED_MIB=72000
  [ "$MODEL" = "air" ] && NEED_MIB=245000
  if [ "$TOTAL_MIB" -lt "$NEED_MIB" ]; then
    echo
    echo "ERROR: MODEL=$MODEL needs about ${NEED_MIB} MiB of VRAM in total,"
    echo "       but this allocation has ${TOTAL_MIB} MiB."
    echo
    if [ "$MODEL" = "flash" ]; then
      echo "       GLM-4.7-Flash weights alone are ~56 GiB, so a 40 GB A100"
      echo "       cannot hold them. Request an 80 GB card:"
      echo
      echo "         sbatch --gres=gpu:a100:1 --constraint=a100@80g --mem=120G \\"
      echo "                --cpus-per-task=16 --export=ALL,MODEL=flash \\"
      echo "                run_inference_test.sbatch"
    else
      echo "       GLM-4.5-Air is ~221 GiB in bf16 and needs 4 x A100 80GB."
      echo "       Every general-access GPU partition caps GRES at one GPU per"
      echo "       job, so this needs the multigpu partition, which requires"
      echo "       prior approval:"
      echo
      echo "         sbatch --partition=multigpu --gres=gpu:a100:4 --mem=380G \\"
      echo "                --cpus-per-task=16 --export=ALL,MODEL=air \\"
      echo "                run_inference_test.sbatch"
    fi
    echo
    echo "       Or use the small model, which runs anywhere:"
    echo "         sbatch run_inference_test.sbatch"
    exit 1
  fi
  # Measured at TP=2 on 80 GB cards: 28.08 GiB of weights per GPU and 41.21
  # GiB of KV cache per GPU. At TP=1 the weights take ~56 GiB, leaving ~12.5
  # GiB -- about 124k KV tokens, so 32k of context is comfortable. A ticket
  # plus the prompt is a few thousand tokens.
  if [ "$TP" = "1" ]; then
    export MAX_LEN="${MAX_LEN:-32768}"
  fi
fi

echo "=== TP=${TP}${MAX_LEN:+, max_model_len=$MAX_LEN} ==="

# --- vLLM on a free loopback port ------------------------------------------
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()')"
LOG="logs/vllm-${SLURM_JOB_ID:-local}.log"

echo
echo "=== starting vLLM on 127.0.0.1:${PORT} (log: $LOG) ==="
# MAX_LEN is already exported above when it needs to be set, so it is inherited
# here. Do NOT reintroduce a `${MAX_LEN:+MAX_LEN="$MAX_LEN"}` prefix: bash
# recognises assignments before a command at parse time, so an expansion that
# produces `MAX_LEN=32768` is treated as the command word instead, and the job
# dies with "MAX_LEN=32768: command not found".
MODEL="$MODEL" PORT="$PORT" VLLM_HOST=127.0.0.1 TP="$TP" \
  "$TM_ROOT/cluster/serve_glm.sh" > "$LOG" 2>&1 &
VLLM_PID=$!

cleanup() {
  [ -n "${VLLM_PID:-}" ] && kill "$VLLM_PID" 2>/dev/null
  wait 2>/dev/null
}
trap cleanup EXIT INT TERM

echo -n "waiting for the model to load"
READY=0
for _ in $(seq 1 120); do          # up to ~20 minutes
  if ! kill -0 "$VLLM_PID" 2>/dev/null; then
    echo
    echo "ERROR: vLLM exited during startup. Last 40 lines of $LOG:"
    echo "----------------------------------------------------------------------"
    tail -40 "$LOG"
    exit 1
  fi
  if curl -sf --noproxy '*' --max-time 3 \
       "http://127.0.0.1:${PORT}/v1/models" -o /dev/null 2>/dev/null; then
    READY=1; echo " ready"; break
  fi
  echo -n "."
  sleep 10
done

if [ "$READY" != 1 ]; then
  echo
  echo "ERROR: vLLM never became ready. Last 40 lines of $LOG:"
  echo "----------------------------------------------------------------------"
  tail -40 "$LOG"
  exit 1
fi

# --- the actual test --------------------------------------------------------
echo
ARGS=(--base-url "http://127.0.0.1:${PORT}/v1" --ticket "$TICKET")
[ -n "$INSTRUCTIONS" ] && ARGS+=(--instructions "$INSTRUCTIONS")

python "$TM_ROOT/tests/inference_test.py" "${ARGS[@]}"
STATUS=$?

echo
echo "======================================================================"
if [ "$STATUS" = 0 ]; then
  echo " RESULT: PASS -- inference works and the prompt produced a valid draft"
else
  echo " RESULT: FAIL -- see the failures above; vLLM log is $LOG"
fi
echo "======================================================================"
exit "$STATUS"
