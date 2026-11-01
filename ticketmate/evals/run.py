"""Evaluate rc-agent against the ground truth (evals/groundtruth.yaml).

    backend/.venv/bin/python evals/run.py                     rag + routing + guardrails (no GPU)
    backend/.venv/bin/python evals/run.py --suite rag         one suite
    backend/.venv/bin/python evals/run.py --suite e2e         full drafts (needs vLLM: VLLM_BASE_URL)
    backend/.venv/bin/python evals/run.py --compare <report.json>   show the change from a baseline
    ... --gate                                                 exit 1 if a gated metric is below its floor

Suites
  rag         retrieval on each case's own words: Hit@k, MRR, recall@k against
              expected_sources; forbidden pages retrieved; low_confidence
              precision and recall; context size. Starts a private rc_kg if
              RC_KG_URL is not set.
  routing     skill triggers against the labels; the orchestrator's rules
              against `route` where the rules decide (the rest needs the model).
  guardrails  input guardrails on the adversarial cases.
  e2e         the whole draft through the backend (agent graph or, with
              AGENT_MODE=false, the pipeline): route accuracy, skills used,
              lookups made, retrieval on the final evidence, reply checks
              (must / must not / asks for what is missing), critic and
              reviewer verdicts, the reviewer's supported share (groundedness),
              guardrail findings, latency, tokens, similarity to the reference.

Reports go to traces/$USER/evals/<time>-<suite>/ (report.json, report.md,
rows.jsonl). Labels a case does not have are not scored for it.
"""

import argparse
import asyncio
import difflib
import json
import os
import re
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

HERE = Path(__file__).resolve().parent
TM = HERE.parent
sys.path.insert(0, str(TM / "backend"))

# RC's playbooks are internal and live outside the repository (cluster/env.sh sets PLAYBOOK_DIR).
import os as _os  # noqa: E402
_PRIVATE = "/projects/rc/projects/rc-copilot/shared/playbooks"
if not _os.environ.get("PLAYBOOK_DIR") and _os.path.isdir(_PRIVATE):
    _os.environ["PLAYBOOK_DIR"] = _PRIVATE
if not _os.environ.get("PLAYBOOK_DIR"):
    print("SKIP: RC's playbooks are not available here (set PLAYBOOK_DIR)")
    raise SystemExit(0)

from app import guardrails, kg_client, ops_store, ticket_context  # noqa: E402
from app.agents import skills as skills_mod  # noqa: E402

GROUNDTRUTH = HERE / "groundtruth.yaml"
K = 6
# --gate floors (the metric must be >= the floor; *_rate metrics where lower is better use ceilings).
FLOORS = {"rag.hit_at_k": 0.85, "rag.forbidden_rate": ("<=", 0.0), "rag.low_conf_recall": 0.8,
          "routing.skill_accuracy": 0.9, "routing.rule_route_accuracy": 1.0, "guardrails.detected": 1.0,
          "e2e.route_accuracy": 0.85, "e2e.reply_checks_pass": 0.85, "e2e.forbidden_claims": ("<=", 0.0)}


# --- Cases ---------------------------------------------------------------------

def load_cases(path: Path = GROUNDTRUTH) -> List[Dict[str, Any]]:
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    d = spec.get("defaults") or {}
    out = []
    for c in spec["cases"]:
        c = dict(c)
        c["_never"] = list(d.get("never_sources") or [])
        c["_reply_must"] = list(d.get("reply_must") or []) + list(c.get("reply_must") or [])
        c["_reply_must_not"] = list(d.get("reply_must_not") or []) + list(c.get("reply_must_not") or [])
        out.append(c)
    return out


def _email(who: str) -> str:
    last, _, first = who.partition(",")
    return "%s.%s@northeastern.edu" % (first.strip()[:1].lower(), re.sub(r"\s+", "", last.strip().lower()))


def compose(case: Dict[str, Any]) -> str:
    """The case as the ServiceNow page the extension scrapes (activity newest first)."""
    head = "Ticket: GT%s\n\nShort description: %s\n\nDescription:\n%s\n" % (
        re.sub(r"\D", "", case["id"]) or "0001", case.get("subject", ""), case.get("description", "").rstrip())
    thread = case.get("thread") or []
    if not thread:
        return head
    staff = [t["who"] for t in thread if t.get("staff")]
    team = ", ".join(sorted({_email(w) for w in staff} | {"rc.one@northeastern.edu", "rc.two@northeastern.edu",
                                                          "rc.three@northeastern.edu"}))
    entries = ["System\nEmail sent•10/20/2026 08:00:00 AM\nEmail sent\nSubject:\nIncident has had comments added.\n"
               "From:\nNortheastern University\nTo:\n%s\nShow email details" % team]
    for i, t in enumerate(thread):
        kind = "Work notes" if t.get("kind") == "work_note" else "Additional comments (Customer Viewable Field)"
        entries.append("%s\n%s•10/%02d/2026 0%d:00:00 AM\n%s" % (t["who"], kind, 21 + i, 1 + i % 8, t["body"].rstrip()))
    return head + "\nWork notes / activity:\n" + "\n".join(reversed(entries)) + "\n"


def _page(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


# --- rc_kg for the rag suite ---------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def ensure_kg() -> Optional[subprocess.Popen]:
    """Use RC_KG_URL, or start a private rc_kg (scripts/serve_kg.sh) for this run."""
    if os.environ.get("RC_KG_URL"):
        return None
    port = _free_port()
    os.environ.setdefault("RC_KG_API_TOKEN", "eval-%d" % os.getpid())
    os.environ["RC_KG_REFRESH_LOOP"] = "off"          # an eval never rebuilds or switches the graph
    proc = subprocess.Popen([str(TM.parent / "rc-kg" / "scripts" / "serve_kg.sh"), "--port", str(port)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    os.environ["RC_KG_URL"] = "http://127.0.0.1:%d" % port
    from app.config import get_settings
    get_settings.cache_clear()
    req = urllib.request.Request(os.environ["RC_KG_URL"] + "/health",
                                 headers={"Authorization": "Bearer " + os.environ["RC_KG_API_TOKEN"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(150):
        try:
            if json.loads(opener.open(req, timeout=2).read()).get("status") == "ok":
                return proc
        except Exception:                               # noqa: BLE001
            pass
        time.sleep(2)
    proc.terminate()
    raise SystemExit("rc_kg did not start")


# --- Scoring -------------------------------------------------------------------

def score_retrieval(case, pages: List[str], low: bool) -> Dict[str, Any]:
    row: Dict[str, Any] = {}
    exp = [_page(u) for u in case.get("expected_sources") or []]
    if exp:
        ranks = [pages.index(p) + 1 for p in exp if p in pages]
        row.update(hit=bool(ranks), rr=1.0 / min(ranks) if ranks else 0.0,
                   recall=len(ranks) / len(exp))
    banned = {_page(u) for u in (case.get("must_not_sources") or []) + case["_never"]}
    row["forbidden"] = sorted(set(pages) & banned)
    if "low_confidence" in case:
        row["low_conf_expected"], row["low_conf_got"] = bool(case["low_confidence"]), bool(low)
    return row


def score_reply(case, reply: str) -> Dict[str, Any]:
    failed = [p for p in case["_reply_must"] if not re.search(p, reply, re.M)]
    failed += ["not: " + p for p in case["_reply_must_not"] if re.search(p, reply, re.M)]
    asks = [p for p in case.get("must_ask") or [] if not (re.search(p, reply) and "?" in reply)]
    out = {"reply_failed": failed, "asks_missing": asks}
    if case.get("reference_reply"):
        out["similarity"] = round(difflib.SequenceMatcher(None, reply, case["reference_reply"]).ratio(), 3)
    return out


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None


def summarise_retrieval(rows) -> Dict[str, Any]:
    with_exp = [r for r in rows if "hit" in r]
    lc = [r for r in rows if "low_conf_expected" in r]
    tp = sum(1 for r in lc if r["low_conf_expected"] and r["low_conf_got"])
    fp = sum(1 for r in lc if not r["low_conf_expected"] and r["low_conf_got"])
    fn = sum(1 for r in lc if r["low_conf_expected"] and not r["low_conf_got"])
    return {"cases": len(rows), "hit_at_k": _mean([1.0 if r["hit"] else 0.0 for r in with_exp]),
            "mrr": _mean([r["rr"] for r in with_exp]), "recall_at_k": _mean([r["recall"] for r in with_exp]),
            "forbidden_rate": _mean([1.0 if r["forbidden"] else 0.0 for r in rows]),
            "low_conf_precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "low_conf_recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "context_chars_median": statistics.median([r["context_chars"] for r in rows]) if rows else None}


# --- Suites --------------------------------------------------------------------

async def suite_rag(cases) -> Dict[str, Any]:
    rows = []
    for c in cases:
        if not (c.get("expected_sources") or c.get("must_not_sources") or "low_confidence" in c):
            continue
        prepared = ticket_context.prepare(compose(c))
        kg = await kg_client.retrieve_multi(prepared.query or prepared.text, prepared.expansion,
                                            (prepared.query or "").split("\n", 1)[0])
        if kg.status != "ok":
            rows.append({"id": c["id"], "error": kg.detail or kg.status})
            continue
        pages = [_page(s["url"]) for s in kg.sources]     # every source the writer gets (<= 8)
        row = {"id": c["id"], "query_chars": len(prepared.query), "expansion": prepared.expansion,
               "sources": len(kg.sources), "context_chars": len(kg.context),
               "top": [s.get("page_title", "") + " > " + s.get("heading", "") for s in kg.sources[:3]]}
        row.update(score_retrieval(c, pages, kg.low_confidence))
        rows.append(row)
    ok = [r for r in rows if "error" not in r]
    return {"metrics": summarise_retrieval(ok), "rows": rows}


async def suite_routing(cases) -> Dict[str, Any]:
    from app.agents import graph
    from app.agents.state import Evidence
    rows, skill_ok, route_ok = [], [], []
    for c in cases:
        text = compose(c)
        prepared = ticket_context.prepare(text)
        matched = skills_mod.trigger(prepared.query, prepared.text, c.get("instructions") or "")
        names = {m.playbook.name for m in matched}
        row = {"id": c["id"], "triggered": sorted(names)}
        if "skills" in c:
            row["skills_ok"] = set(c["skills"]) <= names
            skill_ok.append(row["skills_ok"])
        if c.get("skills_not"):
            row["skills_not_ok"] = not (set(c["skills_not"]) & names)
            skill_ok.append(row["skills_not_ok"])
        if "route" in c:
            # The rules only: a model would decide the rest (the e2e suite scores that).
            from app import llm_client

            async def no_model(*a, **k):
                raise RuntimeError("rules only")
            real, llm_client.chat = llm_client.chat, no_model
            try:
                r = await graph._route({"ticket": prepared.text, "query": prepared.query,
                                        "instructions": c.get("instructions") or "", "ticket_number": "",
                                        "evidence": Evidence(skills=matched)})
            finally:
                llm_client.chat = real
            decided = r["route_reason"].startswith("rules")
            row.update(route=r["route"], decided_by_rules=decided)
            if decided:
                row["route_ok"] = r["route"] == c["route"]
                route_ok.append(row["route_ok"])
        rows.append(row)
    return {"metrics": {"skill_accuracy": _mean([1.0 if x else 0.0 for x in skill_ok]),
                        "rule_route_accuracy": _mean([1.0 if x else 0.0 for x in route_ok]),
                        "rule_routed_share": round(len(route_ok) / max(1, sum(1 for c in cases if "route" in c)), 3)},
            "rows": rows}


async def suite_guardrails(cases) -> Dict[str, Any]:
    rows, hits = [], []
    for c in cases:
        _, found = guardrails.check_input(ticket_context.prepare(compose(c)).text)
        got = {f.rule for f in found}
        want = set(c.get("guardrails") or [])
        row = {"id": c["id"], "found": sorted(got)}
        if want:
            row["detected"] = want <= got
            hits.append(row["detected"])
        elif got:
            row["false_positive"] = sorted(got)
        rows.append(row)
    return {"metrics": {"detected": _mean([1.0 if x else 0.0 for x in hits]),
                        "false_positives": sum(1 for r in rows if r.get("false_positive"))}, "rows": rows}


async def suite_e2e(cases) -> Dict[str, Any]:
    os.environ.setdefault("RUN_LOG_DIR", "")
    from app import main as backend
    from app.models import DraftRequest
    rows = []
    for c in cases:
        t0 = time.perf_counter()
        resp = await backend.draft(DraftRequest(ticket_text=compose(c), extra_instructions=c.get("instructions") or "",
                                                ticket_number="GT-" + c["id"]))
        if not hasattr(resp, "draft_response"):
            rows.append({"id": c["id"], "error": "HTTP %s" % getattr(resp, "status_code", "?")})
            continue
        ctx = resp.context or {}
        ag = ctx.get("agent") or {}
        row = {"id": c["id"], "latency_s": round(time.perf_counter() - t0, 1),
               "tokens": (resp.usage or {}).get("total_tokens"), "route": ag.get("route"),
               "revisions": ag.get("revisions"), "reviews_ok": ag.get("reviews_ok"),
               "tool_calls": ag.get("tool_calls"), "skills_used": ctx.get("playbooks"),
               "caveats_guardrail": [x for x in resp.caveats if x.startswith("Guardrail:")]}
        if "route" in c:
            row["route_ok"] = ag.get("route") == c["route"] if ag else None
        if "skills" in c:
            row["skills_ok"] = set(c["skills"]) <= set(ctx.get("playbooks") or [])
        if c.get("must_lookup"):
            made = {"%s:%s" % (lk.kind, lk.target) for lk in resp.lookups}
            row["lookups_recall"] = round(sum(1 for x in c["must_lookup"] if x in made) / len(c["must_lookup"]), 3)
        pages = [_page(r.url) for r in resp.references]
        row.update(score_retrieval(c, pages, False))
        row.pop("low_conf_got", None)
        row.update(score_reply(c, resp.draft_response))
        rows.append(row)
    ok = [r for r in rows if "error" not in r]
    return {"metrics": {
        "cases": len(rows), "errors": len(rows) - len(ok),
        "route_accuracy": _mean([1.0 if r["route_ok"] else 0.0 for r in ok if r.get("route_ok") is not None]),
        "skill_recall": _mean([1.0 if r["skills_ok"] else 0.0 for r in ok if "skills_ok" in r]),
        "lookup_recall": _mean([r["lookups_recall"] for r in ok if "lookups_recall" in r]),
        "cited_hit": _mean([1.0 if r["hit"] else 0.0 for r in ok if "hit" in r]),
        "reply_checks_pass": _mean([0.0 if (r["reply_failed"] or r["asks_missing"]) else 1.0 for r in ok]),
        "forbidden_claims": sum(1 for r in ok for f in r["reply_failed"] if f.startswith("not:")),
        "reviews_passed": _mean([1.0 if r["reviews_ok"] else 0.0 for r in ok if r["reviews_ok"] is not None]),
        "revised_share": _mean([1.0 if r["revisions"] else 0.0 for r in ok if r["revisions"] is not None]),
        "latency_s_median": statistics.median([r["latency_s"] for r in ok]) if ok else None,
        "tokens_median": statistics.median([r["tokens"] for r in ok if r["tokens"]]) if ok else None,
        "similarity_median": statistics.median([r["similarity"] for r in ok if "similarity" in r])
        if any("similarity" in r for r in ok) else None}, "rows": rows}


SUITES = {"rag": suite_rag, "routing": suite_routing, "guardrails": suite_guardrails, "e2e": suite_e2e}


# --- Reports -------------------------------------------------------------------

def flatten(report) -> Dict[str, Any]:
    return {"%s.%s" % (s, k): v for s, body in report["suites"].items() for k, v in body["metrics"].items()}


def gate(report) -> List[str]:
    flat, bad = flatten(report), []
    for key, floor in FLOORS.items():
        if key not in flat or flat[key] is None:
            continue
        op, val = floor if isinstance(floor, tuple) else (">=", floor)
        ok = flat[key] <= val if op == "<=" else flat[key] >= val
        if not ok:
            bad.append("%s = %s (needs %s %s)" % (key, flat[key], op, val))
    return bad


def markdown(report, baseline=None) -> str:
    flat, base = flatten(report), flatten(baseline) if baseline else {}
    lines = ["# rc-agent evaluation -- %s" % report["at"], "",
             "Ground truth: %d cases (`evals/groundtruth.yaml`). Mode: %s. Versions: %s" % (
                 report["cases"], report["mode"], json.dumps(report["versions"])), "",
             "| metric | value |" + (" baseline | change |" if base else ""),
             "|---|---|" + ("---|---|" if base else "")]
    for k, v in flat.items():
        row = "| %s | %s |" % (k, v)
        if base:
            b = base.get(k)
            delta = round(v - b, 3) if isinstance(v, (int, float)) and isinstance(b, (int, float)) else ""
            row += " %s | %s |" % (b, delta)
        lines.append(row)
    bad = gate(report)
    lines += ["", "Gate: " + ("PASS" if not bad else "FAIL -- " + "; ".join(bad))]
    for s, body in report["suites"].items():
        lines += ["", "## %s" % s, ""]
        for r in body["rows"]:
            flags = [k for k in ("forbidden", "reply_failed", "asks_missing") if r.get(k)]
            if r.get("hit") is False or r.get("route_ok") is False or r.get("skills_ok") is False or flags \
                    or r.get("detected") is False or r.get("error") or \
                    ("low_conf_expected" in r and r.get("low_conf_expected") != r.get("low_conf_got")):
                lines.append("- **%s**: %s" % (r["id"], json.dumps({k: v for k, v in r.items() if k != "id"})[:400]))
    return "\n".join(lines) + "\n"


async def amain() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--suite", action="append", choices=list(SUITES), help="default: rag, routing, guardrails")
    ap.add_argument("--compare", help="a previous report.json")
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--groundtruth", default=str(GROUNDTRUTH))
    args = ap.parse_args()
    suites = args.suite or ["rag", "routing", "guardrails"]
    cases = load_cases(Path(args.groundtruth))
    kg_proc = ensure_kg() if any(s in ("rag", "e2e") for s in suites) else None
    try:
        from app.agents import graph
        from app.config import get_settings
        report = {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "cases": len(cases),
                  "mode": "agent" if get_settings().agent_mode else "pipeline",
                  "versions": {k: len(v) for k, v in graph.manifest().items()}, "manifest": graph.manifest(),
                  "suites": {}}
        for s in suites:
            report["suites"][s] = await SUITES[s](cases)
    finally:
        if kg_proc is not None:
            kg_proc.terminate()
    baseline = json.loads(Path(args.compare).read_text()) if args.compare else None
    out = ops_store.new_eval_dir("+".join(suites))
    md = markdown(report, baseline)
    if out is not None:
        (out / "report.json").write_text(json.dumps(report, indent=1, default=str))
        (out / "report.md").write_text(md)
        with open(out / "rows.jsonl", "w") as fh:
            for s, body in report["suites"].items():
                for r in body["rows"]:
                    fh.write(json.dumps(dict(r, suite=s), default=str) + "\n")
    print(md.split("\n## ")[0])
    print("report: %s" % out)
    return 1 if (args.gate and gate(report)) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(amain()))
