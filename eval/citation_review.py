"""Citation accuracy on real TicketMate drafts.

eval/run.py measures retrieval: is the right page among the sources. This
measures what the model then did with them, from TicketMate's run logs
(/scratch/$USER/rc_copilot_runs, one JSON per draft, holding the exact prompt
and the raw model output with its [S#] markers still in it):

  - how often grounded drafts cite at all
  - for each citation, the retrieval/support.py verdict: ok, better_source
    (another retrieved page fits the sentence clearly better) or weak
  - [S#] markers that name no source, and URLs the model wrote itself

and writes every citation to a CSV with an empty `human_label` column, so a
person can mark a sample right/wrong and the support check's real error rate
can be measured instead of assumed.

Run:  scripts/with_kg.sh -m eval.citation_review [RUN_DIR] [--out FILE.csv]

The CSV quotes draft sentences, which can carry researcher details from the
ticket; it is written mode 600 next to the run logs, not on /projects.
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional

from retrieval.support import check

_SOURCE = re.compile(r"^\[(S\d+)\] (.+)\nURL: (\S+)", re.M)
_MARKER = re.compile(r"\[S\d+(?:\s*,\s*S\d+)*\]")
_URL = re.compile(r"https?://\S+")


def _texts(raw: str) -> Optional[List[str]]:
    try:
        start, end = raw.index("{"), raw.rindex("}") + 1
        d = json.loads(raw[start:end])
    except ValueError:
        return None
    return ([d.get("draft_response", ""), d.get("problem_summary", "")]
            + list(d.get("suggested_steps", [])) + list(d.get("caveats", [])))


def review(run_dir: Path, out_csv: Path, only: str = "") -> Dict:
    stats: Counter = Counter()
    verdicts: Counter = Counter()
    rows = []
    for path in sorted(run_dir.rglob("*.json")):
        if only and not path.parent.name.startswith(only.upper()):
            continue
        try:
            run = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        stats["runs"] += 1
        user = next((m["content"] for m in run.get("prompt") or [] if m["role"] == "user"), "")
        sources = {m.group(1): (m.group(2), m.group(3)) for m in _SOURCE.finditer(user)}
        texts = _texts(run.get("raw_response") or "")
        if not sources or texts is None:
            continue
        stats["grounded_drafts"] += 1
        joined = "\n".join(texts)
        if _MARKER.search(joined):
            stats["drafts_citing"] += 1
        stats["unknown_refs"] += sum(
            1 for m in _MARKER.finditer(joined)
            for ref in re.findall(r"S\d+", m.group(0)) if ref not in sources)
        stats["model_written_urls"] += len(_URL.findall(joined))

        refs = {ref: url for ref, (_, url) in sources.items()}
        for v in check(texts, refs):
            verdicts[v.verdict] += 1
            rows.append({
                "run": str(path.relative_to(run_dir)),
                "model": run.get("model", ""),
                "ref": v.ref,
                "verdict": v.verdict,
                "claim": v.claim,
                "cited_title": sources[v.ref][0],
                "cited_url": sources[v.ref][1],
                "better_title": sources[v.best_ref][0] if v.best_ref else "",
                "score": v.score,
                "best_score": v.best_score if v.best_score is not None else "",
                "human_label": "",
            })

    if rows:
        fd = os.open(out_csv, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
    n = sum(verdicts.values())
    return {
        **stats,
        "citations": n,
        "verdicts": dict(verdicts),
        "flagged_pct": round(100.0 * (n - verdicts["ok"]) / n, 1) if n else None,
        "cite_rate_pct": round(100.0 * stats["drafts_citing"] / stats["grounded_drafts"], 1)
        if stats["grounded_drafts"] else None,
        "csv": str(out_csv) if rows else None,
    }


def main() -> int:
    default = Path("/scratch") / os.environ.get("USER", "unknown") / "rc_copilot_runs"
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("run_dir", nargs="?", default=str(default))
    ap.add_argument("--out", help="CSV path (default: <run_dir>/citation_review.csv)")
    ap.add_argument("--only", default="", help="ticket-number prefix, e.g. RCKG-EVAL")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    run_dir = Path(args.run_dir)
    if not run_dir.is_dir():
        print("no run logs at %s" % run_dir, file=sys.stderr)
        return 1
    print(json.dumps(review(run_dir, Path(args.out or run_dir / "citation_review.csv"),
                            args.only), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
