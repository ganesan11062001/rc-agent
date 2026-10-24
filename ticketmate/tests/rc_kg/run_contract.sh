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
RC_KG_ROOT="${RC_KG_ROOT:-$(cd "$TM_ROOT/../rc-kg" && pwd)}"
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
import json, sys, time, urllib.request, urllib.error
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
h = wait(A, "ok")
check(h is not None, "health: knowledge_graph ok")
st = (h or {}).get("startup") or {}
check(st.get("ready") is True and st.get("vllm", {}).get("stage") == "ready"
      and st.get("knowledge_graph", {}).get("stage") == "ready", "health: startup reports ready once both are up")
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
check(run.get("trace_version") == 3 and tr.get("request_id") == d.get("request_id") and d.get("request_id"),
      "trace_version 3, same request_id as the API response")
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
check(att.get("outcome") == "ok" and att.get("raw_response") and att.get("latency_ms") is not None and att.get("run_id"),
      "final attempt: raw output, latency, outcome, link to its llm run")
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

print("A. run tree (LangSmith model) and feedback")
rt = tr.get("run_tree") or {}
runs = rt.get("runs") or []
byid = {x["id"]: x for x in runs}
roots = [x for x in runs if x.get("parent_id") is None]
check(len(roots) == 1 and roots[0]["name"] == "draft" and roots[0]["run_type"] == "chain"
      and roots[0]["status"] == "success" and rt.get("root_run_id") == roots[0]["id"], "one root chain run 'draft'")
check(all(x.get("parent_id") is None or x["parent_id"] in byid for x in runs), "every run's parent exists")
check(all(x.get("end_time") and x.get("latency_ms") is not None and x.get("status") in ("success", "error") for x in runs),
      "every run finished, with latency and status")
names = {x["name"] for x in runs}
check({"rc_kg.retrieve", "generate_draft", "llm", "parse_draft", "rc_kg.cite", "tokenize.prompt_breakdown"} <= names,
      "steps present: retrieve, generate_draft, llm, parse_draft, cite, tokenize")
derived = [x for x in runs if (x.get("metadata") or {}).get("derived")]
check({"rc_kg.embed_query", "rc_kg.vector_search", "rc_kg.fulltext_search"} <= {x["name"] for x in derived}
      and all(byid[x["parent_id"]]["name"] == "rc_kg.retrieve" for x in derived), "rc_kg's stages nested under retrieve")
ret_run = next(x for x in runs if x["name"] == "rc_kg.retrieve")
check(ret_run["run_type"] == "retriever" and ret_run["outputs"].get("documents")
      and all(dct.get("page_content") and dct["metadata"].get("url") for dct in ret_run["outputs"]["documents"]),
      "retriever run: documents with content and metadata")
llm_run = byid.get(att.get("run_id")) or {}
check(llm_run.get("run_type") == "llm" and llm_run.get("inputs", {}).get("messages")
      and llm_run.get("outputs", {}).get("completion") and llm_run.get("usage") == att.get("usage")
      and (llm_run.get("metadata") or {}).get("invocation_params", {}).get("temperature") is not None,
      "llm run: messages, completion, invocation params, same usage as the attempt")
m = roots[0].get("metadata") or {}
check(m.get("project") == "rc-copilot/full" and m.get("thread_id") and m.get("session_id")
      and (m.get("prompt_version") or {}).get("version"), "project, thread, session, prompt version on the root")
fbk = {f["key"] for f in rt.get("feedback") or []}
check({"grounded", "references", "citations_flagged", "urls_removed"} <= fbk, "automatic feedback attached")
tok_ = call(A, "/api/login", {"access_token": "contract"})["session_token"]
fbr = call(A, "/api/feedback", {"request_id": d["request_id"], "key": "thumbs", "score": 1, "comment": "good"}, tok_)
lines = open(logs[-1].replace(".json", ".feedback.jsonl")).read().splitlines() if logs else []
check(fbr.get("feedback_id") and len(lines) == 1 and json.loads(lines[0])["key"] == "thumbs", "POST /api/feedback stored beside the run")
try:
    call(A, "/api/feedback", {"request_id": "00000000", "key": "thumbs", "score": 1}, tok_); notfound = False
except urllib.error.HTTPError as e:
    notfound = e.code == 404
check(notfound, "feedback for an unknown draft is 404")

print("A. evaluators: assertions, metrics, LLM judge, people's feedback")
ev = d.get("evaluations") or {}
check(len(ev.get("assertions") or {}) >= 9 and "assert_signoff" in ev["assertions"], "assertions returned with the draft")
check(ev["assertions"].get("assert_signoff") is True and ev["assertions"].get("assert_no_placeholder") is True,
      "sign-off and no-placeholder rules pass on a compliant draft")
check("assert_urls_from_sources" in (ev.get("assertions_failed") or []),
      "a draft that wrote an unretrieved URL fails assert_urls_from_sources")
mt = ev.get("metrics") or {}
check(0 <= mt.get("conciseness", -1) <= 1 and -1 <= mt.get("answer_relevance_embedding", -2) <= 1
      and mt.get("hallucinated_urls") == 1, "metrics: conciseness, embedding relevance, hallucinated URLs")
srcs = {f.get("source") for f in rt.get("feedback") or []}
check({"auto", "assertion", "metric"} <= srcs, "trace feedback from auto, assertion and metric evaluators")
fbfile = logs[-1].replace(".json", ".feedback.jsonl")
judged = {}
for _ in range(40):
    judged = {}
    for line in open(fbfile).read().splitlines():
        f = json.loads(line)
        if f.get("source") == "llm_judge":
            judged[f["key"]] = f
    if len(judged) >= 4: break
    time.sleep(0.5)
check(set(judged) >= {"hallucination", "correctness", "conciseness", "answer_relevance"},
      "background LLM judge recorded hallucination, correctness, conciseness, answer_relevance")
check(judged.get("hallucination", {}).get("lower_is_better") is True
      and (judged.get("correctness", {}).get("metadata") or {}).get("usage"),
      "judge grades say their direction and carry the judge call's tokens")
sent = d["draft_response"].replace("Hi,", "Hello,")
call(A, "/api/feedback", {"request_id": d["request_id"], "key": "comment", "score": None,
                         "comment": "fine", "correction": sent}, tok_)
recs = [json.loads(l) for l in open(fbfile).read().splitlines()]
es = [f for f in recs if f["key"] == "edit_similarity"]
check(any(f["key"] == "comment" and f.get("score") is None for f in recs), "comment-only feedback accepted")
check(len(es) == 1 and 0.9 < es[0]["score"] < 1, "reply as sent -> edit_similarity (small edit, just under 1)")

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
lr = [x for x in ((tr.get("run_tree") or {}).get("runs") or []) if x["run_type"] == "llm"]
check([x["status"] for x in lr] == ["error", "success"] and "unsupported" in (lr[0].get("error") or ""),
      "run tree: rejected call is an error run, the fallback a success run")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
PY
