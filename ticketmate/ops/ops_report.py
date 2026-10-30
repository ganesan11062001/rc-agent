"""AIOps / LLMOps report: how the app has been doing, from traces/$USER/metrics/.

    backend/.venv/bin/python ops/ops_report.py              the last 7 days
    backend/.venv/bin/python ops/ops_report.py --days 30 --user all

One row per draft is written by the backend (app/main.py -> app/ops_store.py);
this summarises them: volume, the route mix, latency (p50/p95, by stage),
tokens, how often the agents' tools were refused, how often the critic and
reviewer passed drafts and how often they sent one back, the reviewer's
groundedness (supported share), guardrails that fired, retrieval health
(knowledge graph up, low-confidence rate, URLs removed), the assertions that
failed most, and which prompt/skill/tool versions were live. Written to
traces/$USER/evals/<time>-ops/ops.md as well as printed.
"""

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import ops_store  # noqa: E402


def pct(xs, q):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    return xs[min(len(xs) - 1, int(round(q * (len(xs) - 1))))]


def rate(rows, f):
    rows = list(rows)
    return round(sum(1 for r in rows if f(r)) / len(rows), 3) if rows else None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--user", default="", help="'all' for every user's traces")
    args = ap.parse_args()
    roots = sorted((ops_store.REPO_ROOT / "traces").glob("*")) if args.user == "all" else [ops_store.root()]
    since = datetime.now() - timedelta(days=args.days)
    rows = []
    for root in roots:
        for f in sorted((root / "metrics").glob("*.jsonl")):
            try:
                if datetime.strptime(f.stem, "%Y-%m-%d") < since - timedelta(days=1):
                    continue
            except ValueError:
                continue
            for line in f.read_text().splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    pass
    if not rows:
        print("no drafts in the last %d days" % args.days)
        return 0
    agent = [r for r in rows if r.get("mode") == "agent"]
    lat = [r.get("latency_ms") for r in rows]
    stages = Counter()
    for r in rows:
        for k, v in (r.get("timings_ms") or {}).items():
            if isinstance(v, (int, float)):
                stages[k] += v
    tok = [((r.get("tokens") or {}).get("total_tokens")) for r in rows]
    guard = Counter(g for r in rows for g in r.get("guardrails") or [])
    tools = Counter(t for r in agent for t in r.get("tool_calls") or [])
    failed = Counter(a for r in rows for a in r.get("assertions_failed") or [])
    shares = [r.get("reviewer_supported_share") for r in agent if r.get("reviewer_supported_share") is not None]
    lines = [
        "# rc-agent ops report -- last %d days (%s)" % (args.days, datetime.now().strftime("%Y-%m-%d %H:%M")), "",
        "| | |", "|---|---|",
        "| drafts | %d (%d agent, %d pipeline) over %d tickets |" % (len(rows), len(agent), len(rows) - len(agent),
                                                                 len({r.get("ticket") for r in rows})),
        "| routes | %s |" % dict(Counter(r.get("route") for r in agent)),
        "| latency | p50 %s s, p95 %s s |" % (round((pct(lat, .5) or 0) / 1000, 1), round((pct(lat, .95) or 0) / 1000, 1)),
        "| time by stage (mean ms) | %s |" % {k: int(v / len(rows)) for k, v in stages.most_common(6)},
        "| tokens per draft | p50 %s, p95 %s |" % (pct(tok, .5), pct(tok, .95)),
        "| agent stopped | %s |" % dict(Counter(r.get("agent_stopped") for r in agent)),
        "| tool calls | %s per draft; refused %s |" % (round(sum(tools.values()) / max(1, len(agent)), 2),
                                                     sum(r.get("tool_refusals") or 0 for r in agent)),
        "| tools used | %s |" % dict(tools.most_common(8)),
        "| reviews passed (last round) | %s |" % rate(agent, lambda r: all((r.get("reviews") or {}).values())),
        "| drafts revised | %s |" % rate(agent, lambda r: (r.get("revisions") or 0) > 0),
        "| reviewer groundedness | median %s |" % (round(statistics.median(shares), 2) if shares else None),
        "| guardrails fired | %s |" % (dict(guard) or "none"),
        "| knowledge graph | ok %s, low confidence %s, URLs removed %s |" % (
            rate(rows, lambda r: r.get("kg_status") == "ok"), rate(rows, lambda r: r.get("low_confidence")),
            sum(r.get("urls_removed") or 0 for r in rows)),
        "| skills used | %s |" % dict(Counter(p for r in rows for p in r.get("playbooks") or []).most_common(8)),
        "| assertions failed | %s |" % (dict(failed.most_common(6)) or "none"),
        "| models | %s |" % dict(Counter(r.get("model") for r in rows)),
        "| prompt versions | %s |" % dict(Counter(r.get("prompt_version") for r in rows)),
    ]
    text = "\n".join(lines) + "\n"
    print(text)
    out = ops_store.new_eval_dir("ops")
    if out is not None:
        (out / "ops.md").write_text(text)
        print("report: %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
