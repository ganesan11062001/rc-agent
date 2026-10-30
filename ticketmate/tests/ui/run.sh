#!/usr/bin/env bash
# Browser tests: the web UI's evaluation display and feedback form, and the
# extension panel's result layout.
#
#   tests/ui/run.sh        exit 0 = pass; screenshots in $SHOTS (default /tmp)
#
# Needs Playwright with Chromium, kept out of the backend venv:
#   python3 -m venv /scratch/$USER/pw_venv && /scratch/$USER/pw_venv/bin/pip install playwright
#   PLAYWRIGHT_BROWSERS_PATH=/scratch/$USER/pw_browsers /scratch/$USER/pw_venv/bin/playwright install chromium
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; TM="$(cd "$HERE/../.." && pwd)"
RC_KG_ROOT="${RC_KG_ROOT:-$(cd "$TM/../rc-kg" && pwd)}"
PW_PYTHON="${PW_PYTHON:-/scratch/$USER/pw_venv/bin/python}"
export PLAYWRIGHT_BROWSERS_PATH="${PLAYWRIGHT_BROWSERS_PATH:-/scratch/$USER/pw_browsers}"
[ -x "$PW_PYTHON" ] || { echo "no Playwright python at $PW_PYTHON (see header)" >&2; exit 2; }
SHOTS="${SHOTS:-/tmp}"; WORK="$(mktemp -d)"; chmod 700 "$WORK"; PIDS=()
# Stop every process this test started and remove its work dir.
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
# A loopback port that is free right now.
free() { python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'; }
KGP=$(free); VP=$(free); AP=$(free); mkdir -m 700 "$WORK/runs"
export RC_KG_API_TOKEN="ui-$$"
# Never rebuild or switch the shared graph, and keep spans and metrics out of
# the real traces/$USER/.
export RC_KG_REFRESH_LOOP=off RC_TRACES_DIR="$WORK/traces"
SLURM_JOB_ID=uitest "$RC_KG_ROOT/scripts/serve_kg.sh" --port "$KGP" > "$WORK/kg.log" 2>&1 & PIDS+=($!)
python3 "$TM/tests/rc_kg/stub_vllm.py" "$VP" & PIDS+=($!)
( cd "$TM/backend" && RC_KG_URL="http://127.0.0.1:$KGP" VLLM_BASE_URL="http://127.0.0.1:$VP/v1" VLLM_MODEL_NAME=stub-model \
  AUTH_TOKEN=uitest LOG_FILE= RUN_LOG_DIR="$WORK/runs" exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$AP" ) \
  > "$WORK/app.log" 2>&1 & PIDS+=($!)
for _ in $(seq 1 150); do
  python3 -c "import json,sys,urllib.request as u; o=u.build_opener(u.ProxyHandler({})); sys.exit(0 if json.load(o.open('http://127.0.0.1:$AP/api/health',timeout=3))['knowledge_graph']=='ok' else 1)" 2>/dev/null && break
  sleep 2
done
"$PW_PYTHON" "$HERE/web_feedback_test.py" "$AP" "$WORK/runs" "$SHOTS"; rc=$?
# The extension panel's result layout (no backend needed: the worker is mocked).
"$PW_PYTHON" "$HERE/extension_result_test.py" "$SHOTS" || rc=1
# The extension reading catalog request forms (mocked worker too).
"$PW_PYTHON" "$HERE/extension_form_test.py" || rc=1
exit $rc
