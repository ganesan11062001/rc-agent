#!/usr/bin/env bash
# A session's startup as the extension sees it, without a GPU.
#
#   tests/startup/run_slow_start.sh      exit 0 = progress reported throughout
#
# Real rc_kg (writing its checklist) + a fake vLLM that replays a real vLLM
# 0.29 startup log over ~40 s before a stub model answers. /api/health is
# polled every second; the run checks that `startup` moves forward, the graph
# checklist completes, and the session ends up ready. ~1 min.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TM_ROOT="$(cd "$HERE/../.." && pwd)"
RC_KG_ROOT="${RC_KG_ROOT:-$(cd "$TM_ROOT/../rc-kg" && pwd)}"
PY=python3
WORK="$(mktemp -d)"; PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
free() { $PY -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }

export RC_KG_API_TOKEN="slow-$$" RC_KG_PROGRESS_DIR="$WORK/kg_progress" VLLM_LOG_FILE="$WORK/vllm.log"
mkdir -m 700 "$RC_KG_PROGRESS_DIR"
KGP="$(free)"; "$RC_KG_ROOT/scripts/serve_kg.sh" --port "$KGP" >"$WORK/kg.log" 2>&1 & PIDS+=($!)
VP="$(free)"; AP="$(free)"
( cd "$TM_ROOT/backend" && RC_KG_URL="http://127.0.0.1:$KGP" VLLM_BASE_URL="http://127.0.0.1:$VP/v1" \
    VLLM_MODEL_NAME=stub-model AUTH_TOKEN=slow LOG_FILE= USE_PROXY_ENV=false \
    exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$AP" ) >"$WORK/app.log" 2>&1 & PIDS+=($!)
# The fake vLLM: the real log, a line every ~0.25 s, then the stub serves.
# It starts once the backend answers, as on a real node (the backend comes up first).
( until curl -sf --noproxy '*' -o /dev/null "http://127.0.0.1:$AP/api/health"; do sleep 0.5; done
  while IFS= read -r line; do printf '%s\n' "$line"; sleep 0.25; done < "$HERE/vllm-0.29-startup.log" > "$VLLM_LOG_FILE"
  exec $PY "$TM_ROOT/tests/rc_kg/stub_vllm.py" "$VP" ) & PIDS+=($!)

$PY - "$AP" "${RC_SLOW_START_TIMELINE:-}" <<'PY'
import json, sys, time, urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
AP, out = sys.argv[1], sys.argv[2]
seen, t0 = [], time.time()
while time.time() - t0 < 240:
    try:
        h = json.load(op("http://127.0.0.1:%s/api/health" % AP, timeout=5))
    except Exception:
        time.sleep(1); continue
    st = h.get("startup") or {}
    v, k = st.get("vllm", {}), st.get("knowledge_graph", {})
    seen.append({"t": round(time.time() - t0), "status": h["status"], "kg": h["knowledge_graph"], "startup": st})
    print("%4ds  model %-28s %5.1f%%  %-34s | graph %-30s %3s%%" % (
        seen[-1]["t"], v.get("label"), v.get("pct", 0), (v.get("detail") or "")[:34], k.get("label"), k.get("pct")))
    if st.get("ready"):
        break
    time.sleep(1)
if out:
    json.dump(seen, open(out, "w"), indent=1)
fails = []
def check(c, what):
    print(("  ok    " if c else "  FAIL  ") + what)
    if not c: fails.append(what)
starting = [s for s in seen if s["status"] != "ok"]
check(all("startup" in s and s["startup"] for s in seen), "every /api/health carries startup")
check(len({s["startup"]["vllm"]["stage"] for s in starting}) >= 4, "several model stages reported while loading: %s"
      % sorted({s["startup"]["vllm"]["stage"] for s in starting}))
pcts = [s["startup"]["vllm"]["pct"] for s in seen]
check(all(a <= b for a, b in zip(pcts, pcts[1:])), "the model bar never moves backwards")
check(not any(s["startup"]["vllm"]["failed"] for s in seen), "never reported as failed")
kgsteps = [st["status"] for s in seen for st in s["startup"]["knowledge_graph"].get("steps", [])]
check("running" in kgsteps, "graph checklist seen with a step running")
check(any(s["startup"]["knowledge_graph"]["stage"] == "ready" for s in seen), "the graph became ready")
check(bool(seen) and seen[-1]["startup"].get("ready") and seen[-1]["status"] == "ok", "the session ended ready")
print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
PY
