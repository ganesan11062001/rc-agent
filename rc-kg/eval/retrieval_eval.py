"""Tier 1 retrieval evaluation: RAGAS context metrics + ranking metrics.

No LLM. Relevance comes from eval/questions.yaml (expected page URLs), so it
runs anywhere the graph does and can gate a build. See eval/metrics.py for the
formulas and which of them match RAGAS exactly.

  scripts/with_kg.sh -m eval.retrieval_eval
  scripts/with_kg.sh -m eval.retrieval_eval -k 5 --json report.json
  RC_KG_VARIANT=website scripts/with_kg.sh -m eval.retrieval_eval --sites rc.northeastern.edu

Every mean comes with a 95% bootstrap interval over questions: at this eval
size (tens of questions), differences inside the interval are not evidence.
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

import yaml

from retrieval.graphrag import retrieve

from . import metrics as m

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"
DOCS = "rc-docs.northeastern.edu"


def _page(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _host(url: str) -> str:
    return url.split("/")[2] if "://" in url else ""


def _group(expected: List[str]) -> str:
    hosts = {_host(u) for u in expected}
    return "docs" if hosts == {DOCS} else ("website" if DOCS not in hosts else "both")


def run(k: int = 5, sites=None) -> Dict:
    qs = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    if sites:
        qs = [q for q in qs if any(_host(u) in sites for u in q["expected_urls"])]
    rows = []
    for q in qs:
        expected = [_page(u) for u in q["expected_urls"]]
        res = retrieve(q["question"], k=k)
        pages = [_page(s.page_url) for s in res.sources]
        rows.append({"question": q["question"], "group": _group(q["expected_urls"]),
                     "retrieved": pages, "expected": expected,
                     **m.per_query(pages, expected, k)})
    names = [n for n in rows[0] if n not in ("question", "group", "retrieved", "expected")] if rows else []

    def summarize(rs):
        out = {"n": len(rs)}
        for n in names:
            point, lo, hi = m.bootstrap_ci([r[n] for r in rs])
            out[n] = {"mean": round(point, 3), "ci95": [round(lo, 3), round(hi, 3)]}
        return out

    groups = sorted({r["group"] for r in rows})
    return {"k": k, "overall": summarize(rows),
            "by_expected_site": {g: summarize([r for r in rows if r["group"] == g]) for g in groups},
            "rows": rows}


def render(rep: Dict) -> str:
    k = rep["k"]
    cols = ["hit@1", "hit@%d" % k, "mrr", "ndcg@%d" % k,
            "context_precision", "id_context_precision", "id_context_recall"]
    head = "%-14s %3s  " % ("questions", "n") + "  ".join("%-20s" % c for c in cols)
    lines = [head, "-" * len(head)]

    def line(name, s):
        cells = ["%.3f [%.2f,%.2f]" % (s[c]["mean"], *s[c]["ci95"]) for c in cols]
        return "%-14s %3d  " % (name, s["n"]) + "  ".join("%-20s" % c for c in cells)

    lines.append(line("all", rep["overall"]))
    for g, s in rep["by_expected_site"].items():
        lines.append(line("  " + g, s))
    misses = [r["question"] for r in rep["rows"] if not r["hit@%d" % k]]
    lines.append("\nmisses at k=%d: %s" % (k, misses or "none"))
    lines.append("RAGAS id_context_recall needs every listed page; the labels list "
                 "alternatives, so read hit@k for 'found an answer'.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--sites", default="")
    ap.add_argument("--json", help="write the full report (with per-question rows) here")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    rep = run(args.k, [s for s in args.sites.split(",") if s] or None)
    print(render(rep))
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
