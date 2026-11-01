#!/usr/bin/env bash
# The release gate: every test that needs no GPU, in one command.
#
#   tests/run_all.sh            all of it (~3 min, most of it rc_kg starting)
#   tests/run_all.sh --quick    unit tests only (seconds)
#
# Exit 0 only if every suite passes. Run it before publishing a release
# (ood-apps/publish.sh) and after any change to prompts, skills or tools.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TM="$(cd "$HERE/.." && pwd)"; KG="$(cd "$TM/../rc-kg" && pwd)"
PY="$TM/backend/.venv/bin/python"
rc=0
step() {   # step <name> <command...>
  local name="$1"; shift
  local out; out="$("$@" 2>&1)"; local s=$?
  printf '%-34s %s\n' "$name" "$([ $s = 0 ] && echo PASS || echo "FAIL (exit $s)")"
  [ $s = 0 ] || { echo "$out" | grep -E "FAIL|Error|error" | head -15 | sed 's/^/    /'; rc=1; }
}
step "ticket context"        "$PY" "$TM/tests/ticket_context/test_ticket_context.py"
step "cluster lookups"       "$PY" "$TM/tests/lookups/test_lookups.py"
step "skills (playbooks)"    "$PY" "$TM/tests/playbooks/test_playbooks.py"
step "request forms"         "$PY" "$TM/tests/playbooks/test_forms.py"
step "sign-off, judge, readable trace" "$PY" "$TM/tests/judge/test_judge.py"
step "agent graph"           "$PY" "$TM/tests/agent/test_agent.py"
step "OpenTelemetry export"  "$PY" "$TM/tests/agent/test_otel.py"
step "guardrails"            "$PY" "$TM/tests/agent/test_guardrails.py"
step "prompt lint"           "$PY" "$TM/tests/agent/test_prompts.py"
step "eval: routing+guardrails" "$PY" "$TM/evals/run.py" --suite routing --suite guardrails --gate
step "rc-kg unit tests"      "$KG/.venv/bin/python" -m pytest -q "$KG/tests"
# Both apps must run the same knowledge-graph code (they publish to one shared store).
step "rc-kg same as TicketMate's" bash -c "diff -rq --exclude=__pycache__ --exclude=.venv --exclude=.venv-eval --exclude=vendor --exclude=data --exclude=.env --exclude=.pytest_cache '$KG' /projects/rc/projects/rc-copilot/ticketmate/rc-kg"
if [ "${1:-}" != "--quick" ]; then
  step "rc-kg ticket regressions" bash -c "cd '$KG' && scripts/with_kg.sh -m eval.ticket_cases"
  step "eval: RAG on ground truth" "$PY" "$TM/evals/run.py" --suite rag --gate
  step "end-to-end contract (A-G)" "$TM/tests/rc_kg/run_contract.sh"
fi
echo; [ $rc = 0 ] && echo "ALL PASS" || echo "SOME FAILED"
exit $rc
