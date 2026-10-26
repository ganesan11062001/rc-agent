"""Website-only graph vs website + rc-docs, on the same questions and tickets.

The build gate scores each variant on the questions it *can* answer (the
website graph skips docs-only questions), which says nothing about which one
to run. This scores both on the same everything:

  1. eval/questions.yaml, all 45: Hit@1/3/k and MRR, overall and split by
     where the answer lives -- website, rc-docs, or both;
  2. eval/ticket_cases.yaml, the reviewed real-ticket regressions;
  3. optionally real recorded tickets (TicketMate run logs): no right answer
     is known, so it reports how retrieval behaves -- best vector score,
     low_confidence, where sources come from, prompt size. Only aggregates
     are written; no ticket text.

Run once per variant (inside a private copy of that graph), then compare:

  RC_KG_VARIANT=full    scripts/with_kg.sh -m eval.variants collect /tmp/full.json    [--runs DIR]
  RC_KG_VARIANT=website scripts/with_kg.sh -m eval.variants collect /tmp/website.json [--runs DIR]
  .venv/bin/python -m eval.variants compare /tmp/website.json /tmp/full.json
"""

import argparse
import glob
import json
import os
import sys
from pathlib import Path
from statistics import mean, median
from typing import Dict, List, Optional

import yaml

from retrieval.graphrag import retrieve

from .ticket_cases import check as ticket_cases

HERE = Path(__file__).resolve().parent
WEBSITE, DOCS = "rc.northeastern.edu", "rc-docs.northeastern.edu"


def _page(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else ""


def _where(urls: List[str]) -> str:
    hosts = {_host(u) for u in urls}
    return "both" if hosts >= {WEBSITE, DOCS} else ("docs" if hosts == {DOCS} else "website")


def _ticket_query(run: Dict) -> Optional[str]:
    """What TicketMate actually sent rc-kg for this draft: the rc_kg.retrieve
    run's input, else (older logs) the ticket text."""
    for r in ((run.get("trace") or {}).get("run_tree") or {}).get("runs", []):
        if r.get("name") == "rc_kg.retrieve" and (r.get("inputs") or {}).get("query"):
            return r["inputs"]["query"]
    return (run.get("input") or {}).get("ticket_text")


def collect(out: str, k: int, runs_dir: Optional[str]) -> None:
    """Score the graph currently served (variant from RC_KG_VARIANT) on the
    questions, the ticket cases and, if `runs_dir` is given, each distinct
    recorded ticket query; write the per-item results (no ticket text) as
    JSON to `out`."""
    variant = os.environ.get("RC_KG_VARIANT", "full")
    qs = yaml.safe_load((HERE / "questions.yaml").read_text(encoding="utf-8"))["questions"]
    questions = []
    for q in qs:
        expected = {_page(u) for u in q["expected_urls"]}
        res = retrieve(q["question"], k=k)
        pages = [_page(s.page_url) for s in res.sources]
        questions.append({
            "where": _where(q["expected_urls"]),
            "rank": next((i for i, p in enumerate(pages, 1) if p in expected), None),
            "best_vector": max((s.vector_score or 0.0 for s in res.sources), default=0.0),
            "low_confidence": res.low_confidence,
        })
    sites = None if variant == "full" else [WEBSITE]
    cases = [{"id": c["id"], "ok": c["ok"], "problems": c["problems"]} for c in ticket_cases(k=k, sites=sites)]

    tickets = []
    if runs_dir:
        seen = set()
        for f in sorted(glob.glob(os.path.join(runs_dir, "*", "*.json"))):
            try:
                run = json.load(open(f, encoding="utf-8"))
            except (ValueError, OSError):
                continue
            query = _ticket_query(run)
            if not query or query in seen:
                continue
            seen.add(query)
            res = retrieve(query, k=k)
            tickets.append({
                "best_vector": max((s.vector_score or 0.0 for s in res.sources), default=0.0),
                "low_confidence": res.low_confidence,
                "docs_sources": sum(1 for s in res.sources if s.site == DOCS),
                "sources": len(res.sources),
                "context_chars": len(res.context),
            })
    json.dump({"variant": variant, "k": k, "questions": questions, "ticket_cases": cases,
               "tickets": tickets}, open(out, "w"), indent=1)
    print("%s: %d questions, %d ticket cases, %d real tickets -> %s"
          % (variant, len(questions), len(cases), len(tickets), out))


def _scores(rows: List[Dict], k: int) -> Dict[str, float]:
    """Hit@1/3/k, MRR and low_confidence rate over collected question rows."""
    n = len(rows) or 1
    return {"n": len(rows),
            "hit@1": sum(1 for r in rows if r["rank"] == 1) / n,
            "hit@3": sum(1 for r in rows if r["rank"] and r["rank"] <= 3) / n,
            "hit@%d" % k: sum(1 for r in rows if r["rank"]) / n,
            "mrr": sum(1.0 / r["rank"] for r in rows if r["rank"]) / n,
            "low_conf": sum(1 for r in rows if r["low_confidence"]) / n}


def compare(paths: List[str]) -> None:
    """Print the collect() files side by side, one column per variant."""
    data = [json.load(open(p)) for p in paths]
    k = data[0]["k"]
    names = [d["variant"] for d in data]
    w = max(12, *(len(n) for n in names))

    def row(label, vals, fmt="%.3f"):
        """Print one labelled row with a value per variant ("-" for None)."""
        print("  %-34s" % label + "".join(("%" + str(w) + "s") % (fmt % v if v is not None else "-") for v in vals))

    print("\n1. Same 45 questions for both graphs (page-level hits, k=%d)" % k)
    print("  %-34s" % "" + "".join(("%" + str(w) + "s") % n for n in names))
    for subset, label in ((None, "all questions"), ("website", "answer on the website"),
                          ("docs", "answer only in rc-docs"), ("both", "answer on both sites")):
        sc = [_scores([q for q in d["questions"] if subset is None or q["where"] == subset], k) for d in data]
        print("  -- %s (%d)" % (label, sc[0]["n"]))
        for m in ("hit@1", "hit@3", "hit@%d" % k, "mrr"):
            row("   " + m, [s[m] for s in sc])
        row("   flagged low_confidence", [s["low_conf"] for s in sc], "%.2f")

    print("\n2. Reviewed ticket cases (eval/ticket_cases.yaml)")
    row("passed", [sum(c["ok"] for c in d["ticket_cases"]) for d in data], "%d")
    for i, c in enumerate(data[0]["ticket_cases"]):
        status = ["ok" if d["ticket_cases"][i]["ok"] else "FAIL" for d in data]
        if len(set(status)) > 1 or "FAIL" in status:
            print("    %-32s" % c["id"][:32] + "".join(("%" + str(w) + "s") % s for s in status))

    if all(d["tickets"] for d in data):
        print("\n3. Real recorded tickets (%d; no right answer known -- behaviour only)" % len(data[0]["tickets"]))
        row("best vector score, median", [median(t["best_vector"] for t in d["tickets"]) for d in data])
        row("low_confidence raised", [sum(t["low_confidence"] for t in d["tickets"]) for d in data], "%d")
        row("sources from rc-docs (of %d)" % k, [mean(t["docs_sources"] for t in d["tickets"]) for d in data], "%.1f")
        row("context chars per ticket, mean", [mean(t["context_chars"] for t in d["tickets"]) for d in data], "%.0f")


def main() -> int:
    """CLI entry point: the `collect` and `compare` subcommands."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("out")
    c.add_argument("-k", type=int, default=6, help="TicketMate's rc_kg_k")
    c.add_argument("--runs", help="TicketMate run-log dir, e.g. /scratch/$USER/rc_copilot_runs")
    p = sub.add_parser("compare")
    p.add_argument("files", nargs="+")
    a = ap.parse_args()
    if a.cmd == "collect":
        collect(a.out, a.k, a.runs)
    else:
        compare(a.files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
