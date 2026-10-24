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
# D. the website-only graph variant: grounded in rc.northeastern.edu only.
# E. a model server that rejects json_schema: the fallback attempt and the
#    rejected one are both in the trace, and only real usage is counted.
# Needs no GPU. ~1-2 min, almost all of it rc_kg starting.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TM_ROOT="$(cd "$HERE/../.." && pwd)"
RC_KG_ROOT="${RC_KG_ROOT:-/projects/rc/projects/rc_kg}"
PY=python3
WORK="$(mktemp -d)"; PIDS=()
# backend() runs inside $(...), a subshell, so its PIDs cannot go into PIDS;
# it appends them to a file the cleanup reads instead (an array silently
# lost them, leaking four backends per run).
cleanup() {
  for p in "${PIDS[@]}" $(cat "$WORK/backend.pids" 2>/dev/null); do kill "$p" 2>/dev/null; done
  wait 2>/dev/null
  # RC_CONTRACT_KEEP_RUNS=<dir>: keep the run logs (e.g. to try cluster/traces.py).
  [ -n "${RC_CONTRACT_KEEP_RUNS:-}" ] && [ -d "$WORK/runs" ] && cp -r "$WORK/runs/." "$RC_CONTRACT_KEEP_RUNS/"
  rm -rf "$WORK"
}
trap cleanup EXIT
free() { $PY -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1]); s.close()'; }

backend() {   # backend <kg_url> [run_log_dir] [vllm_port] -> prints port
  local port; port="$(free)"
  ( cd "$TM_ROOT/backend" && RC_KG_URL="$1" VLLM_BASE_URL="http://127.0.0.1:${3:-$VP}/v1" \
      VLLM_MODEL_NAME=stub-model AUTH_TOKEN=contract LOG_FILE= RUN_LOG_DIR="${2:-}" USE_PROXY_ENV=false \
      exec .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "$port" ) \
      >"$WORK/app-$port.log" 2>&1 &
  echo $! >> "$WORK/backend.pids"; echo "$port"
}

VP="$(free)"; $PY "$HERE/stub_vllm.py" "$VP" & PIDS+=($!)
VRP="$(free)"; STUB_REJECT_SCHEMA=1 $PY "$HERE/stub_vllm.py" "$VRP" & PIDS+=($!)
export RC_KG_API_TOKEN="contract-$$"
KGP="$(free)"; "$RC_KG_ROOT/scripts/serve_kg.sh" --port "$KGP" >"$WORK/kg.log" 2>&1 & PIDS+=($!)
KWP="$(free)"; "$RC_KG_ROOT/scripts/serve_kg.sh" --port "$KWP" --variant website >"$WORK/kg-website.log" 2>&1 & PIDS+=($!)
FKP="$(free)"; $PY "$HERE/fake_kg_v2.py" "$FKP" & PIDS+=($!)
A="$(backend "http://127.0.0.1:$KGP" "$WORK/runs")"
B="$(backend "http://127.0.0.1:$FKP")"
C="$(backend "http://127.0.0.1:$(free)")"      # nothing listens there
W="$(backend "http://127.0.0.1:$KWP")"
E="$(backend "http://127.0.0.1:$KGP" "$WORK/runs-e" "$VRP")"

$PY - "$A" "$B" "$C" "$W" "$WORK/runs" "$E" "$WORK/runs-e" <<'PY'
import json, sys, time, urllib.request
op = urllib.request.build_opener(urllib.request.ProxyHandler({})).open
A, B, C, W, RUNS, E, RUNS_E = sys.argv[1:]
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
print("A. trace of that draft (run log)")
import glob, os
logs = sorted(glob.glob(os.path.join(RUNS, "*", "*.json")), key=os.path.getmtime)
check(len(logs) == 1, "one run log written")
run = json.load(open(logs[-1])) if logs else {}
tr = run.get("trace") or {}
check(run.get("trace_version") == 2 and tr.get("request_id") == d.get("request_id") and d.get("request_id"),
      "trace_version 2, same request_id as the API response")
tok = tr.get("tokens") or {}
check(isinstance(tok.get("total_tokens"), int) and tok["total_tokens"] > 0
      and tok["total_tokens"] == tok["prompt_tokens"] + tok["completion_tokens"], "token totals add up")
check(d.get("usage", {}).get("total_tokens") == tok.get("total_tokens"), "API usage matches the run log")
check(tok.get("model_calls", 0) >= 1 and tok.get("final_call") and tok.get("calls_with_usage") == tok.get("model_calls"),
      "every model call recorded with usage")
bd = tok.get("prompt_breakdown") or {}
check({"system_prompt", "ticket_text", "rc_sources", "template_and_scaffolding"} <= set(bd)
      and bd["rc_sources"] > 0, "prompt token breakdown (incl. RC sources)")
att = ((tr.get("llm") or {}).get("attempts") or [{}])[-1]
check(att.get("outcome") == "ok" and att.get("messages") and att.get("raw_response") and att.get("latency_ms") is not None,
      "final attempt: messages, raw output, latency, outcome")
ret = tr.get("retrieval") or {}
check(ret.get("status") == "ok" and ret.get("sources") and all(x.get("url") for x in ret["sources"]),
      "retrieval: sources with URLs")
check(((ret.get("rc_kg") or {}).get("retrieval") or {}).get("request_id") == d["request_id"],
      "rc_kg's retrieval trace carries the same request_id")
check(((ret.get("rc_kg") or {}).get("snapshot") or {}).get("variant") == "full", "graph version recorded")
cit = tr.get("citations") or {}
check(len(cit.get("references") or []) == len(d["references"]) and cit.get("removed_urls")
      and (cit.get("rc_kg") or {}).get("request_id") == d["request_id"], "citations: references, removed URLs, same request_id")
out = tr.get("output") or {}
check("[S" in json.dumps(out.get("model_draft")) and out.get("response", {}).get("request_id") == d["request_id"],
      "model output before and after citation rendering")
tm = tr.get("timings_ms") or {}
check({"retrieve", "llm", "citations", "total"} <= set(tm), "per-stage timings")
check(bool(((tr.get("versions") or {}).get("ticket_mate") or {}).get("commit")), "TicketMate version recorded")
check(run.get("prompt") and run.get("raw_response") and run.get("input"), "version-1 fields kept for existing readers")

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

print("D. website-only graph variant")
h = wait(W, "ok")
check(h is not None and "RC website graph" in (h.get("knowledge_graph_detail") or ""), "health names the website graph")
d = draft(W, OOM)
check(d["kg_status"] == "ok" and d["references"], "draft grounded")
check(all(r["url"].startswith("https://rc.northeastern.edu/") for r in d["references"]), "every reference is on the RC website")
h = wait(A, "ok")
check(h is not None and "website + docs" in (h.get("knowledge_graph_detail") or ""), "full graph session names the full graph")

print("E. model server rejects json_schema (strategy fallback)")
check(wait(E, "ok") is not None, "health ok")
d = draft(E, OOM)
logs = sorted(glob.glob(os.path.join(RUNS_E, "*", "*.json")))
tr = (json.load(open(logs[-1])).get("trace") or {}) if logs else {}
att = (tr.get("llm") or {}).get("attempts") or []
check([a.get("strategy") for a in att] == ["json_schema", "json_object"], "both attempts recorded, in order")
check(att and att[0].get("outcome") == "unsupported" and att[0].get("http_status") == 400 and att[0].get("usage") is None,
      "rejected attempt: outcome, HTTP status, no usage")
tok = tr.get("tokens") or {}
check(tok.get("model_calls") == 2 and tok.get("calls_with_usage") == 1
      and tok.get("total_tokens") == (att[-1].get("usage") or {}).get("total_tokens"),
      "tokens counted from the call that returned usage")
check(d["kg_status"] == "ok" and d["references"], "draft still grounded after the fallback")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
PY
