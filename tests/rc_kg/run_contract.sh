#!/usr/bin/env bash
# Consumer-side contract test: TicketMate's backend against rc_kg.
#
#   tests/rc_kg/run_contract.sh        exit 0 = all three scenarios pass
#
# A. real rc_kg (scripts/serve_kg.sh from RC_KG_ROOT) + stub model:
#    drafts are grounded, cited [1].., invented URLs removed, a wrong
#    citation flagged, no raw [S#] anywhere.
# B. an rc_kg claiming API v2.0: refused, draft ungrounded with the reason.
# C. rc_kg unreachable: draft still produced, with a caveat.
# Needs no GPU. ~1-2 min, almost all of it rc_kg starting.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TM_ROOT="$(cd "$HERE/../.." && pwd)"
RC_KG_ROOT="${RC_KG_ROOT:-/projects/rc/projects/rc_kg}"
PY=python3
WORK="$(mktemp -d)"; PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; rm -rf "$WORK"; }
trap cleanup EXIT
free() { $PY -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }

backend() {   # backend <kg_url> -> prints port
  local port; port="$(free)"
  ( cd "$TM_ROOT/backend" && RC_KG_URL="$1" VLLM_BASE_URL="http://127.0.0.1:$VP/v1" \
      VLLM_MODEL_NAME=stub-model AUTH_TOKEN=contract LOG_FILE= RUN_LOG_DIR= USE_PROXY_ENV=false \
      exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$port" ) \
      >"$WORK/app-$port.log" 2>&1 &
  PIDS+=($!); echo "$port"
}

VP="$(free)"; $PY "$HERE/stub_vllm.py" "$VP" & PIDS+=($!)
export RC_KG_API_TOKEN="contract-$$"
KGP="$(free)"; "$RC_KG_ROOT/scripts/serve_kg.sh" --port "$KGP" >"$WORK/kg.log" 2>&1 & PIDS+=($!)
FKP="$(free)"; $PY "$HERE/fake_kg_v2.py" "$FKP" & PIDS+=($!)
A="$(backend "http://127.0.0.1:$KGP")"
B="$(backend "http://127.0.0.1:$FKP")"
C="$(backend "http://127.0.0.1:$(free)")"      # nothing listens there

$PY - "$A" "$B" "$C" <<'PY'
import json, sys, time, urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
A, B, C = sys.argv[1:]
fails = []
def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond: fails.append(what)
def call(port, path, body=None, tok=None):
    req = urllib.request.Request("http://127.0.0.1:%s%s" % (port, path),
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + tok} if tok else {})})
    with op(req, timeout=120) as r: return json.loads(r.read())
def wait(port, want, timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            h = call(port, "/api/health")
            if h["knowledge_graph"] == want: return h
        except Exception: pass
        time.sleep(2)
    return None
def draft(port, text):
    tok = call(port, "/api/login", {"access_token": "contract"})["session_token"]
    return call(port, "/api/draft", {"ticket_text": text}, tok)
def all_text(d):
    return "\n".join([d["draft_response"], d["problem_summary"]] + d["suggested_steps"] + d["caveats"])
OOM = "My job was killed, sacct says OUT_OF_MEMORY. I asked for --mem=4G. What should I do?"
GPU = "My GPU job has been pending for hours on the gpu partition, what are the limits?"

print("A. real rc_kg")
check(wait(A, "ok") is not None, "health: knowledge_graph ok")
d = draft(A, OOM)
check(d["kg_status"] == "ok", "kg_status ok")
check(len(d["references"]) >= 1 and all({"n", "title", "url"} <= set(r) for r in d["references"]), "references {n,title,url}")
check("References:" in d["draft_response"] and d["draft_response"].rstrip().endswith("Research Computing"),
      "References block above the sign-off")
check("[S" not in all_text(d), "no raw [S#] markers")
check("made-up-oom-guide" not in d["draft_response"] and any("Removed 1 link" in c for c in d["caveats"]),
      "invented URL removed and reported")
check(not any(c.startswith("Check citation") for c in d["caveats"]), "correct citations not flagged")
g = draft(A, GPU)
check(any(c.startswith("Check citation [2]") for c in g["caveats"]), "wrong citation flagged")
check(not any(c.startswith("Check citation [1]") for c in g["caveats"]), "correct citation not flagged")

print("B. rc_kg with an incompatible API version")
h = wait(B, "incompatible", 30)
check(h is not None and "2.0" in (h.get("knowledge_graph_detail") or ""), "health: incompatible, says which version")
d = draft(B, OOM)
check(d["kg_status"] == "unavailable" and not d["references"], "draft ungrounded")
check(any("API version" in c for c in d["caveats"]), "caveat gives the version reason")
check("[S" not in all_text(d), "no raw [S#] markers")

print("C. rc_kg unreachable")
check(wait(C, "unreachable", 30) is not None, "health: unreachable")
d = draft(C, OOM)
check(d["kg_status"] == "unavailable" and any("unavailable" in c for c in d["caveats"]), "draft with caveat")
check("[S" not in all_text(d), "no raw [S#] markers")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
PY
