#!/usr/bin/env bash
# LLM-judged RAGAS evaluation (eval/ragas_llm.py) with rc-kg started for it.
#
#   scripts/ragas_eval.sh --judge-url http://<node>:<port>/v1 --questions
#   scripts/ragas_eval.sh --judge-url http://<node>:<port>/v1 --runs /scratch/$USER/rc_copilot_runs --only RCKG-EVAL
#   RC_KG_VARIANT=website scripts/ragas_eval.sh --judge-url ... --questions
#
# --questions needs rc-kg, so a private instance is started on a free port
# and stopped afterwards; --runs only reads logs and needs none. Requires
# .venv-eval (scripts/eval_env.sh).
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"
EVAL_PY="$RC_KG_ROOT/.venv-eval/bin/python"
[ -x "$EVAL_PY" ] || kg_die "no .venv-eval; run scripts/eval_env.sh first"

if printf '%s\n' "$@" | grep -qx -- "--questions"; then
  PORT="$(kg_free_port)"
  export RC_KG_API_TOKEN="ragas-$$"
  SLURM_JOB_ID="ragas" "$RC_KG_ROOT/scripts/serve_kg.sh" --port "$PORT" >"${TMPDIR:-/tmp}/rc_kg_ragas_$$.log" 2>&1 &
  S=$!
  trap 'kill $S 2>/dev/null; wait $S 2>/dev/null; rm -f "${TMPDIR:-/tmp}/rc_kg_ragas_$$.log"' EXIT
  for _ in $(seq 1 150); do
    "$RC_KG_PY" -c "import json,sys,urllib.request as u; o=u.build_opener(u.ProxyHandler({})); sys.exit(0 if json.load(o.open('http://127.0.0.1:$PORT/health',timeout=3))['status']=='ok' else 1)" 2>/dev/null && break
    kill -0 $S 2>/dev/null || { cat "${TMPDIR:-/tmp}/rc_kg_ragas_$$.log"; kg_die "rc-kg exited during startup"; }
    sleep 2
  done
  set -- --kg-url "http://127.0.0.1:$PORT" --kg-token "$RC_KG_API_TOKEN" "$@"
fi
"$EVAL_PY" "$RC_KG_ROOT/eval/ragas_llm.py" "$@"
