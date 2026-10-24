#!/usr/bin/env bash
#SBATCH --job-name=rc-capture
#SBATCH --output=logs/rc-capture-%j.out
#SBATCH --error=logs/rc-capture-%j.err
#SBATCH --partition=short
#SBATCH --cpus-per-task=2
#SBATCH --mem=4G
#SBATCH --time=04:00:00
#
# Tests the DATA PATH only: ServiceNow -> extension -> OOD -> Slurm job -> disk.
# No model, and deliberately NO GPU -- this needs none, and the CPU `short`
# partition schedules immediately instead of queueing behind GPU work.
#
#   sbatch capture.sbatch
#   tail -f logs/rc-capture-<jobid>.out
#
# It serves the same API as the real backend, so the browser extension works
# unchanged: paste the URL and token into the popup exactly as normal.

set -uo pipefail

TM_ROOT="${TM_ROOT:-/projects/rc/projects/ticket_mate}"
OOD_HOST="${OOD_HOST:-ood.explorer.northeastern.edu}"
OOD_ROUTE="${OOD_ROUTE:-rnode}"

TOKEN_DIR="$HOME/.rc_copilot"
TOKEN_FILE="$TOKEN_DIR/token"
# Defaults to /scratch/$USER/rc_copilot_captures inside capture_server.py.
export CAPTURE_DIR="${CAPTURE_DIR:-/scratch/$USER/rc_copilot_captures}"

cd "$SLURM_SUBMIT_DIR" || exit 1
mkdir -p logs

NODE="$(hostname -s)"
PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("0.0.0.0",0)); print(s.getsockname()[1]); s.close()')"

# Token in $HOME at mode 600 -- never on group-readable /projects.
mkdir -p "$TOKEN_DIR"; chmod 700 "$TOKEN_DIR"
TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
umask 077
printf '%s\n' "$TOKEN" > "$TOKEN_FILE"
chmod 600 "$TOKEN_FILE"

export AUTH_TOKEN="$TOKEN"
export SESSION_HOURS="${SESSION_HOURS:-4}"
export LOG_FILE=""

PYTHON="$TM_ROOT/backend/.venv/bin/python"
[ -x "$PYTHON" ] || { echo "ERROR: no venv at $PYTHON"; exit 1; }

cat <<BANNER

════════════════════════════════════════════════════════════════════════
 RC Copilot - CAPTURE MODE (no model, no GPU)
════════════════════════════════════════════════════════════════════════
 job        ${SLURM_JOB_ID}
 node       ${NODE}
 port       ${PORT}
 captures   ${CAPTURE_DIR}

 1. In the extension popup, open "Enter connection details manually":

      URL    https://${OOD_HOST}/${OOD_ROUTE}/${NODE}/${PORT}
      token  cat ${TOKEN_FILE}

 2. Open a ServiceNow ticket and click "Draft reply".

 3. The work notes will fill with an echo of exactly what was scraped,
    and the same text is written to disk here. Read it back with:

      ls -lt ${CAPTURE_DIR}
      cat ${CAPTURE_DIR}/<file>.json

 Sanity check from a login node:

      curl --noproxy '*' http://${NODE}:${PORT}/api/health

════════════════════════════════════════════════════════════════════════

BANNER

cd "$(dirname "$0")" || exit 1
exec "$PYTHON" -m uvicorn capture_server:app --host 0.0.0.0 --port "$PORT"
