"""Measure reranking and corrective retrieval before turning them on.

    scripts/with_kg.sh -m eval.rerank_sweep [--json out.json]

One graph, one process, every configuration in CONFIGS over:
  - eval/questions.yaml (45 researcher-style questions): hit@1, hit@k, MRR
  - eval/ticket_cases.yaml (regressions from reviewed tickets): cases passing
  - OFF_TOPIC questions no RC page answers: how often each config flags them
  - latency per query (median, p90)
and, for the reranker, the best source's cross-encoder probability on the
questions vs off-topic ones -- what RERANK_WEAK_BELOW is chosen from.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import yaml

from config import get_settings
from retrieval.corrective import retrieve_corrective

from . import metrics as m
from . import ticket_cases

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"

# Questions a researcher might send that no RC page answers (calibration of
# the "weak" threshold; the same kind of list chose LOW_CONFIDENCE_BELOW).
OFF_TOPIC = [
    "Where can I park on campus overnight?",
    "How do I reset my Canvas course gradebook?",
    "My Outlook calendar is not syncing with my phone",
    "When is the payroll deadline for graduate assistants?",
    "How do I book a study room in Snell Library?",
    "Can I get a refund for my dining plan?",
    "My Husky Card does not open the dorm door",
    "How do I change my legal name in the student system?",
    "The printer in the lab is out of toner",
    "Is the gym open during the winter break?",
    "How do I apply for a travel grant for a conference?",
    "My laptop screen is cracked, where can I get it repaired?",
]

CONFIGS = [
    {"name": "baseline (no rerank)", "rerank_mode": "off", "corrective_rag": False},
    {"name": "MiniLM replace", "rerank_mode": "replace", "rerank_model": "cross-encoder/ms-marco-MiniLM-L-6-v2"},
    {"name": "MiniLM fuse w=1", "rerank_mode": "fuse", "rerank_weight": 1.0,
     "rerank_model": "cross-encoder/ms-marco-MiniLM-L-6-v2"},
    {"name": "MiniLM fuse w=2", "rerank_mode": "fuse", "rerank_weight": 2.0,
     "rerank_model": "cross-encoder/ms-marco-MiniLM-L-6-v2"},
    {"name": "bge-base replace", "rerank_mode": "replace", "rerank_model": "BAAI/bge-reranker-base"},
    {"name": "bge-base fuse w=1", "rerank_mode": "fuse", "rerank_weight": 1.0, "rerank_model": "BAAI/bge-reranker-base"},
]
DEFAULTS = {"rerank_mode": "off", "rerank_model": "cross-encoder/ms-marco-MiniLM-L-6-v2", "rerank_weight": 1.0,
            "rerank_candidates": 20, "corrective_rag": False, "rerank_weak_below": 0.3}


def _page(url):
    return url.split("#", 1)[0].rstrip("/")


def run_config(cfg, k=6):
    s = get_settings()
    for key, val in {**DEFAULTS, **{x: y for x, y in cfg.items() if x != "name"}}.items():
        setattr(s, key, val)
    qs = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    rows, lat, tops, ran = [], [], [], []
    for q in qs:
        t = time.perf_counter()
        res = retrieve_corrective(q["question"], k=k)
        lat.append((time.perf_counter() - t) * 1000)
        pages = [_page(x.page_url) for x in res.sources]
        rows.append(m.per_query(pages, [_page(u) for u in q["expected_urls"]], k))
        if res.trace.get("rerank_top") is not None:
            tops.append(res.trace["rerank_top"])
        c = res.trace.get("corrective") or {}
        if c.get("ran"):
            ran.append("%s [%s -> %s, kept %s]" % (q["question"][:50], c.get("before_top"), c.get("after_top"), c.get("kept")))
    off_flags, off_tops, corrective_ran = 0, [], 0
    for q in OFF_TOPIC:
        res = retrieve_corrective(q, k=k)
        off_flags += bool(res.low_confidence)
        if res.trace.get("rerank_top") is not None:
            off_tops.append(res.trace["rerank_top"])
    cases = ticket_cases.check(k=k)
    mean = lambda key: round(statistics.mean(r[key] for r in rows), 3)
    lat.sort()
    return {"config": cfg["name"], "n": len(rows), "hit@1": mean("hit@1"), "hit@%d" % k: mean("hit@%d" % k),
            "mrr": mean("mrr"), "ticket_cases_ok": sum(c["ok"] for c in cases), "ticket_cases": len(cases),
            "failed_cases": [c["id"] for c in cases if not c["ok"]],
            "off_topic_flagged": "%d/%d" % (off_flags, len(OFF_TOPIC)),
            "latency_ms_p50": round(lat[len(lat) // 2]), "latency_ms_p90": round(lat[int(len(lat) * 0.9)]),
            "corrective_ran": ran,
            "rerank_top_questions": sorted(round(x, 3) for x in tops),
            "rerank_top_off_topic": sorted(round(x, 3) for x in off_tops)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json")
    ap.add_argument("--only", action="append", default=[], help="config name substring")
    ap.add_argument("--extra", default="", help='a JSON list of extra configs, e.g. corrective ones')
    args = ap.parse_args()
    configs = CONFIGS + (json.loads(args.extra) if args.extra else [])
    if args.only:
        configs = [c for c in configs if any(o in c["name"] for o in args.only)]
    out = []
    for cfg in configs:
        r = run_config(cfg)
        out.append(r)
        print("%-24s hit@1 %.3f  hit@6 %.3f  mrr %.3f  cases %d/%d %s  off-topic flagged %s  p50 %dms p90 %dms" % (
            r["config"], r["hit@1"], r["hit@6"], r["mrr"], r["ticket_cases_ok"], r["ticket_cases"],
            r["failed_cases"] or "", r["off_topic_flagged"], r["latency_ms_p50"], r["latency_ms_p90"]), flush=True)
        for line in r["corrective_ran"]:
            print("%24s corrective: %s" % ("", line), flush=True)
        if r["rerank_top_questions"]:
            q, o = r["rerank_top_questions"], r["rerank_top_off_topic"]
            print("%24s rerank_top questions min %.3f p10 %.3f median %.3f | off-topic max %.3f median %.3f" % (
                "", q[0], q[len(q) // 10], q[len(q) // 2], o[-1], o[len(o) // 2]), flush=True)
    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
