"""RAG optimization: sweep the retriever's settings and rank them on every
labelled set at once.

    scripts/with_kg.sh -m eval.optimize                 the default grid
    scripts/with_kg.sh -m eval.optimize --quick         a small grid
    scripts/with_kg.sh -m eval.optimize --json out.json

Measured on three sets, so a setting cannot win on one by losing another:
  questions   eval/questions.yaml (45 researcher-style questions): Hit@5, MRR
  regressions eval/ticket_cases.yaml (reviewed tickets): cases passing
  groundtruth ticketmate/evals/groundtruth.yaml, through TicketMate's own query
              building (ticket_context.prepare): Hit@6, MRR, forbidden pages

The score is MRR on questions + MRR on ground truth, and a setting that
fails a regression case or retrieves a forbidden page is disqualified.
Settings are the module constants in retrieval/graphrag.py, patched per
run; nothing is changed on disk -- copy a winner into graphrag.py by hand,
with the numbers, as the comments there do.
"""

import argparse
import itertools
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List

import yaml

import retrieval.graphrag as g
from eval import run as questions_eval
from eval import ticket_cases

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ticketmate" / "backend"))
from app import ticket_context  # noqa: E402  (pure Python: no backend dependencies)

GRID = {"_RRF_K": [30, 60, 90], "_FULLTEXT_WEIGHT": [0.7, 1.0, 1.4], "_TOPIC_WEIGHT": [0.0, 0.3, 0.6],
        "_MAX_PER_PAGE": [2, 3], "_STALE_FACTOR": [0.6]}
QUICK = {"_RRF_K": [60], "_FULLTEXT_WEIGHT": [0.7, 1.0, 1.4], "_TOPIC_WEIGHT": [0.0, 0.3], "_MAX_PER_PAGE": [2, 3],
         "_STALE_FACTOR": [0.6]}


def _page(u: str) -> str:
    return u.split("#", 1)[0].rstrip("/")


def groundtruth_queries() -> List[Dict]:
    """(query, expected pages, forbidden pages) for each ground-truth case that has RAG labels."""
    sys.path.insert(0, str(ROOT / "ticketmate" / "evals"))
    import run as gt_run                                  # ticketmate/evals/run.py
    out = []
    for c in gt_run.load_cases():
        if not (c.get("expected_sources") or c.get("must_not_sources")):
            continue
        q = ticket_context.prepare(gt_run.compose(c)).query
        out.append({"id": c["id"], "query": q, "expected": {_page(u) for u in c.get("expected_sources") or []},
                    "forbidden": {_page(u) for u in (c.get("must_not_sources") or []) + c["_never"]}})
    return out


def score_groundtruth(cases, k: int = 6) -> Dict:
    rr, hits, forbidden = [], [], 0
    for c in cases:
        pages = [_page(s.page_url) for s in g.retrieve(c["query"], k=k).sources]
        if c["expected"]:
            ranks = [i for i, p in enumerate(pages, 1) if p in c["expected"]]
            rr.append(1.0 / ranks[0] if ranks else 0.0)
            hits.append(1.0 if ranks else 0.0)
        forbidden += bool(set(pages) & c["forbidden"])
    n = max(1, len(rr))
    return {"hit": round(sum(hits) / n, 3), "mrr": round(sum(rr) / n, 3), "forbidden": forbidden}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    grid = QUICK if args.quick else GRID
    original = {k: getattr(g, k) for k in grid}
    gt = groundtruth_queries()
    rows = []
    keys = list(grid)
    for values in itertools.product(*[grid[k] for k in keys]):
        setting = dict(zip(keys, values))
        for k, v in setting.items():
            setattr(g, k, v)
        q = questions_eval.evaluate(k=5)
        cases = ticket_cases.check(6)
        gts = score_groundtruth(gt)
        ok = all(c["ok"] for c in cases) and gts["forbidden"] == 0
        rows.append({"setting": setting, "questions_hit5": round(q["hit@5"], 3), "questions_mrr": round(q["mrr"], 3),
                     "regressions_pass": "%d/%d" % (sum(c["ok"] for c in cases), len(cases)),
                     "gt_hit6": gts["hit"], "gt_mrr": gts["mrr"], "gt_forbidden": gts["forbidden"],
                     "qualified": ok, "score": round(q["mrr"] + gts["mrr"], 3) if ok else None})
        print("%-70s q.hit5=%.3f q.mrr=%.3f reg=%s gt.hit=%.3f gt.mrr=%.3f forb=%d %s" % (
            json.dumps(setting), rows[-1]["questions_hit5"], rows[-1]["questions_mrr"], rows[-1]["regressions_pass"],
            gts["hit"], gts["mrr"], gts["forbidden"], "" if ok else "DISQUALIFIED"), flush=True)
    for k, v in original.items():
        setattr(g, k, v)
    current = next(r for r in rows if all(r["setting"][k] == original[k] for k in keys)) \
        if any(all(r["setting"][k] == original[k] for k in keys) for r in rows) else None
    ranked = sorted((r for r in rows if r["qualified"]), key=lambda r: -r["score"])
    print("\ncurrent: %s" % (json.dumps(current) if current else json.dumps(original)))
    for r in ranked[:5]:
        print("best:    %s" % json.dumps(r))
    if args.json:
        Path(args.json).write_text(json.dumps({"current": current, "ranked": ranked, "all": rows}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
