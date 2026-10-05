#!/usr/bin/env bash
# Where is the vLLM server right now, and how do I reach it?
#
#   ./vllm_status.sh           one-shot report
#   ./vllm_status.sh -w        wait until it is actually serving, then report
#   eval "$(./vllm_status.sh --env)"    export VLLM_* for RC Copilot
#
# The compute node changes with every allocation, so never reuse an old tunnel
# command -- run this instead. Nothing here is hard-coded to a node name.

set -uo pipefail

# Must match the --job-name in the launcher you used. serve_stack.sbatch is the
# only one left, and it sets rc-copilot.
JOB_NAME="${JOB_NAME:-rc-copilot}"
VLLM_PORT="${VLLM_PORT:-8000}"
LOGIN_HOST="${LOGIN_HOST:-login.explorer.northeastern.edu}"
LOCAL_PORT="${LOCAL_PORT:-8000}"   # port on your laptop, the tunnel's left side

WAIT=0
ENV_ONLY=0
NODE_URL_ONLY=0
case "${1:-}" in
  -w|--wait)  WAIT=1 ;;
  --env)      ENV_ONLY=1 ;;
  --node-url) NODE_URL_ONLY=1 ;;   # direct URL, for use from inside the cluster
  -h|--help)  sed -n '2,11p' "$0"; exit 0 ;;
  "")         ;;
  *)          echo "unknown option: $1" >&2; exit 2 ;;
esac

QUIET=$(( ENV_ONLY || NODE_URL_ONLY ))
say() { [ "$QUIET" = 1 ] && return 0; echo "$@"; }
die() { echo "$@" >&2; exit 1; }

models_url() { echo "http://$1:$VLLM_PORT/v1/models"; }

# --noproxy matters: these nodes set http_proxy, which will 403 internal traffic.
probe() { curl -sf --noproxy '*' --max-time 4 "$(models_url "$1")" -o /dev/null; }

served_model() {
  curl -sf --noproxy '*' --max-time 4 "$(models_url "$1")" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null
}

command -v squeue >/dev/null || die "squeue not found -- run this from a login node."

while :; do
  row=$(squeue -u "$USER" -n "$JOB_NAME" -h -o '%i|%T|%N|%L|%R' 2>/dev/null | head -1)

  if [ -z "$row" ]; then
    die "No '$JOB_NAME' job in the queue. Start one with:
  sbatch --gres=gpu:a100:1 --mem=80G --export=ALL,MODEL=qwen7b serve_stack.sbatch"
  fi

  IFS='|' read -r jobid state nodelist timeleft reason <<<"$row"

  if [ "$state" != "RUNNING" ]; then
    say "Job $jobid is $state $reason"
    [ "$WAIT" = 1 ] && { sleep 20; continue; }
    die "Not serving yet. Re-run with -w to wait."
  fi

  node=$(scontrol show hostnames "$nodelist" 2>/dev/null | head -1)
  [ -n "$node" ] || die "Could not resolve a node from '$nodelist'."

  if [ "$NODE_URL_ONLY" = 1 ] && probe "$node"; then
    echo "http://${node}:${VLLM_PORT}/v1"
    exit 0
  fi

  if ! probe "$node"; then
    say "Job $jobid is on $node but the API isn't answering yet (model still loading)."
    [ "$WAIT" = 1 ] && { sleep 20; continue; }
    die "Not serving yet. Re-run with -w to wait, or check logs/${JOB_NAME}-${jobid}.err"
  fi

  break
done

# Ask the server what it calls itself rather than assuming --served-model-name.
model=$(served_model "$node")
[ -n "$model" ] || die "Server answered but returned no model id."

if [ "$ENV_ONLY" = 1 ]; then
  # For use from your laptop, through the tunnel below.
  echo "export VLLM_BASE_URL=http://127.0.0.1:${LOCAL_PORT}/v1"
  echo "export VLLM_MODEL_NAME=${model}"
  exit 0
fi

cat <<EOF

vLLM is serving.
  job       $jobid   ($timeleft remaining)
  node      $node
  model id  $model

1. Tunnel to it from your laptop:

     ssh -N -L ${LOCAL_PORT}:${node}:${VLLM_PORT} ${USER}@${LOGIN_HOST}

2. Run RC Copilot on your laptop against the tunnel:

     export VLLM_BASE_URL=http://127.0.0.1:${LOCAL_PORT}/v1
     export VLLM_MODEL_NAME=${model}
     uvicorn app.main:app --port 8080

3. Open http://127.0.0.1:8080 , or click the RC Copilot extension icon.

EOF
