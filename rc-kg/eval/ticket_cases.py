"""Retrieval regressions from reviewed tickets -- eval/ticket_cases.yaml.

Each case checks what went wrong on a real ticket: pages it must not
retrieve (and pages no case may), a page that must be in the top k, or that
low_confidence is raised when no page covers the problem. Matching is at page
level, as in eval.run. A case whose expected pages are all on a site the graph
does not have is skipped for `expected`, not failed.

Run:  python -m eval.ticket_cases            # failures only, exit 1 if any
      python -m eval.ticket_cases --all      # every case with its top k
"""

import argparse
import logging
import sys
from pathlib import Path
from typing import Dict, List

import yaml

from retrieval.graphrag import retrieve

CASES = Path(__file__).resolve().parent / "ticket_cases.yaml"


def _page(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else ""


def check(k: int = 6, sites=None) -> List[Dict]:
    spec = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    never = {_page(u) for u in spec.get("never", [])}
    out = []
    for case in spec["cases"]:
        res = retrieve(case["query"], k=k)
        pages = [_page(s.page_url) for s in res.sources]
        problems = []
        banned = never | {_page(u) for u in case.get("must_not", [])}
        for p in pages:
            if p in banned:
                problems.append("retrieved %s" % p)
        expected = [_page(u) for u in case.get("expected", [])
                    if not sites or _host(u) in sites]
        if expected and not set(expected) & set(pages):
            problems.append("none of the expected pages in the top %d" % k)
        if case.get("low_confidence") and not res.low_confidence:
            best = max((s.vector_score or 0.0 for s in res.sources), default=0.0)
            problems.append("low_confidence not raised (best vector score %.3f)" % best)
        out.append({"id": case["id"], "why": case.get("why", ""), "ok": not problems,
                    "problems": problems, "low_confidence": res.low_confidence,
                    "top": ["%s > %s" % (s.page_title, s.heading) for s in res.sources]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-k", type=int, default=6, help="TicketMate's rc_kg_k")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--sites", help="comma-separated hosts the graph has")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    rows = check(args.k, args.sites.split(",") if args.sites else None)
    for r in rows:
        if args.all or not r["ok"]:
            print("%s  %s  -- %s" % ("ok  " if r["ok"] else "FAIL", r["id"], r["why"]))
            for p in r["problems"]:
                print("        " + p)
            if args.all:
                for t in r["top"]:
                    print("        . " + t)
    failed = sum(1 for r in rows if not r["ok"])
    print("%d/%d cases pass" % (len(rows) - failed, len(rows)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
