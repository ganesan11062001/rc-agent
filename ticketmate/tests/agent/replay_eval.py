"""Offline eval on real tickets: re-draft past tickets through the agent graph
and report how each was handled.

    # on a GPU job with vLLM and rc_kg running (as the OOD app starts them):
    VLLM_BASE_URL=http://127.0.0.1:8000/v1 RC_KG_URL=http://127.0.0.1:<port> \\
      backend/.venv/bin/python tests/agent/replay_eval.py \\
        --runs /scratch/$USER/rc_copilot_runs --limit 20 --out replay.jsonl

For each ticket (its latest run log): the route and why, what the agent
looked up, whether the critic and the reviewer passed the draft, revisions,
latency and tokens -- and, where the RC engineer recorded the reply they
actually sent, how close the new draft is to it. Compare two reports to see
what a prompt, skill or tool change did before relying on it. Run with
AGENT_MODE=false for the pipeline baseline.

Ticket text stays on this machine: the report holds ticket numbers, routes,
tool names and scores, not the text. Re-drafting writes new run logs only if
RUN_LOG_DIR is set.
"""
import argparse
import asyncio
import difflib
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
os.environ.setdefault("RUN_LOG_DIR", "")          # do not mix replays into the real run logs


def latest_runs(runs_dir: str, limit: int):
    out = []
    for d in sorted(Path(runs_dir).iterdir()):
        logs = sorted(p for p in d.glob("*.json"))
        if logs:
            out.append((d.name, logs[-1]))
    return out[-limit:] if limit else out


def sent_reply(log: Path):
    """The reply the engineer actually sent: the `correction` on their feedback (/api/feedback)."""
    fb = Path(str(log)[:-len(".json")] + ".feedback.jsonl")
    if not fb.exists():
        return None
    for line in reversed(fb.read_text().splitlines()):
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if x.get("source") == "user" and (x.get("correction") or "").strip():
            return x["correction"]
    return None


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--runs", default="/scratch/%s/rc_copilot_runs" % os.environ.get("USER", ""))
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--out", default="replay.jsonl")
    args = ap.parse_args()

    from app import main as backend
    from app.models import DraftRequest
    rows = []
    for ticket, log in latest_runs(args.runs, args.limit):
        rec = json.loads(log.read_text())
        req = DraftRequest(ticket_text=rec["input"]["ticket_text"],
                           extra_instructions=rec["input"].get("extra_instructions") or "",
                           ticket_number=ticket)
        t0 = time.perf_counter()
        try:
            resp = await backend.draft(req)
        except Exception as exc:                     # noqa: BLE001
            rows.append({"ticket": ticket, "error": str(exc)[:200]})
            continue
        if not hasattr(resp, "draft_response"):
            rows.append({"ticket": ticket, "error": "HTTP %s" % getattr(resp, "status_code", "?")})
            continue
        ag = (resp.context or {}).get("agent") or {}
        sent = sent_reply(log)
        rows.append({
            "ticket": ticket, "route": ag.get("route"), "agent_stopped": ag.get("stopped"),
            "tool_calls": ag.get("tool_calls"), "revisions": ag.get("revisions"),
            "reviews_ok": ag.get("reviews_ok"), "kg": resp.kg_status, "references": len(resp.references),
            "confidence": resp.confidence, "playbooks": (resp.context or {}).get("playbooks"),
            "latency_s": round(time.perf_counter() - t0, 1),
            "tokens": (resp.usage or {}).get("total_tokens") if resp.usage else None,
            "similarity_to_sent": round(difflib.SequenceMatcher(None, resp.draft_response, sent).ratio(), 3)
            if sent else None})
        print("%-14s %-9s %-12s calls=%-2s rev=%s ok=%s %ss" % (
            ticket, rows[-1]["route"], rows[-1]["agent_stopped"], len(rows[-1]["tool_calls"] or []),
            rows[-1]["revisions"], rows[-1]["reviews_ok"], rows[-1]["latency_s"]), flush=True)
    with open(args.out, "w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    done = [r for r in rows if "error" not in r]
    if done:
        print("\n%d drafted, %d errors | routes: %s | reviews passed: %d/%d | revised: %d | median %ss" % (
            len(done), len(rows) - len(done),
            {k: sum(1 for r in done if r["route"] == k) for k in {r["route"] for r in done}},
            sum(1 for r in done if r["reviews_ok"]), len(done), sum(1 for r in done if r["revisions"]),
            statistics.median(r["latency_s"] for r in done)))
        sims = [r["similarity_to_sent"] for r in done if r["similarity_to_sent"] is not None]
        if sims:
            print("similarity to the reply actually sent: median %.2f over %d tickets" % (statistics.median(sims), len(sims)))
    print("report: %s" % args.out)
    return 0 if done else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
