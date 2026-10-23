"""Phase 5: retrieval eval -- Hit@k and MRR over eval/questions.yaml.

Matching is at page level: a source is a hit if its page is one of the
question's expected_urls. Section-level judgements would be stricter, but
expected sections would have to be re-labelled every time a page is
re-sectioned, and the page is what an RC engineer links to.

Run:  python -m eval.run            # summary + misses
      python -m eval.run --all      # every question
      python -m eval.run --json out.json --min-hit 0.9   # gate a snapshot build
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

import yaml

from retrieval.graphrag import retrieve

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"


def _page(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else ""


def evaluate(k: int = 5, sites=None) -> Dict:
    """`sites`: only questions with an expected URL on one of these hosts.
    A website-only graph cannot answer a docs-only question, so judging it
    on one would measure the variant's scope, not its retrieval."""
    qs = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    rows: List[Dict] = []
    if sites:
        qs = [q for q in qs if any(_host(u) in sites for u in q["expected_urls"])]
    for q in qs:
        expected = {_page(u) for u in q["expected_urls"]}
        res = retrieve(q["question"], k=k)
        pages = [_page(src.page_url) for src in res.sources]
        rank = next((i for i, p in enumerate(pages, 1) if p in expected), None)
        rows.append({"question": q["question"], "rank": rank, "top": pages[:3],
                     "low_confidence": res.low_confidence})
    n = len(rows)
    return {
        "questions": n,
        "hit@1": sum(1 for r in rows if r["rank"] == 1) / n,
        "hit@%d" % k: sum(1 for r in rows if r["rank"]) / n,
        "mrr": sum(1.0 / r["rank"] for r in rows if r["rank"]) / n,
        "flagged_low_confidence": sum(1 for r in rows if r["low_confidence"]),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--all", action="store_true", help="print every question")
    ap.add_argument("--json", help="also write the summary to this file")
    ap.add_argument("--sites", default="",
                    help="comma-separated hosts; skip questions with no expected URL on them")
    ap.add_argument("--min-hit", type=float, default=None,
                    help="exit 1 if hit@k is below this")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    out = evaluate(args.k, [x for x in args.sites.split(",") if x] or None)
    for r in out["rows"]:
        if args.all or not r["rank"]:
            print("%-5s %s%s" % (r["rank"] or "MISS", r["question"],
                                 "  [low]" if r["low_confidence"] else ""))
            if not r["rank"]:
                for p in r["top"]:
                    print("        got %s" % p)
    summary = {k: (round(v, 3) if isinstance(v, float) else v)
               for k, v in out.items() if k != "rows"}
    print(summary)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2), encoding="utf-8")
    hit = out["hit@%d" % args.k]
    if args.min_hit is not None and hit < args.min_hit:
        print("hit@%d %.3f is below --min-hit %.3f" % (args.k, hit, args.min_hit),
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
