#!/usr/bin/env bash
# Start a fresh rc-kg from the snapshot and check it against API_CONTRACT.md.
#   scripts/contract_test.sh                      exit 0 = the v1 contract holds
#   scripts/contract_test.sh --variant website    against the website-only graph
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"
[ "${1:-}" = "--variant" ] && export RC_KG_VARIANT="$2"
PORT="$(kg_free_port)"
export RC_KG_API_TOKEN="contract-$$"
RC_KG_REFRESH_LOOP=off SLURM_JOB_ID=contract "$RC_KG_ROOT/scripts/serve_kg.sh" --port "$PORT" >"${TMPDIR:-/tmp}/rc_kg_contract_$$.log" 2>&1 &
S=$!
trap 'kill $S 2>/dev/null; wait $S 2>/dev/null; rm -f "${TMPDIR:-/tmp}/rc_kg_contract_$$.log"' EXIT
for _ in $(seq 1 120); do
  "$RC_KG_PY" -c "import json,sys,urllib.request as u; o=u.build_opener(u.ProxyHandler({})); sys.exit(0 if json.load(o.open('http://127.0.0.1:$PORT/health',timeout=3))['status']=='ok' else 1)" 2>/dev/null && break
  kill -0 $S 2>/dev/null || { cat "${TMPDIR:-/tmp}/rc_kg_contract_$$.log"; kg_die "rc-kg exited during startup"; }
  sleep 2
done
"$RC_KG_PY" "$RC_KG_ROOT/eval/contract_check.py" "http://127.0.0.1:$PORT" "$RC_KG_API_TOKEN"
