"""Send the eval questions through a running TicketMate as tickets, so a real
model's grounded drafts land in its run logs for eval/citation_review.py.

  # 1. a TicketMate session with a real model, e.g. from ticketmate/cluster:
  #      sbatch --gres=gpu:a100:1 --mem=80G --cpus-per-task=12 \\
  #             --export=ALL,MODEL=qwen7b serve_stack.sbatch
  #    its log prints the node and port; the token is in ~/.rc_copilot/token
  # 2. python3 eval/draft_batch.py --url http://<node>:<port>
  # 3. scripts/with_kg.sh -m eval.citation_review --only RCKG-EVAL

Standard library only, so any python3 on a login node runs it. Drafts are
filed under ticket numbers RCKG-EVAL-01.. so the review can pick them out.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
_open = urllib.request.build_opener(urllib.request.ProxyHandler({})).open


def _questions(path: Path):
    # Avoid a PyYAML dependency: the file is a flat list of "- question: ..."
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("- question:"):
            out.append(line.split(":", 1)[1].strip().strip('"'))
    return out


def _post(url, body, token=None, timeout=300):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Content-Type": "application/json",
        **({"Authorization": "Bearer " + token} if token else {})})
    with _open(req, timeout=timeout) as r:
        return json.loads(r.read())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", required=True, help="rc-copilot base URL, e.g. http://d1234:45678")
    ap.add_argument("--token-file", default=os.path.expanduser("~/.rc_copilot/token"))
    ap.add_argument("--questions", default=str(HERE / "questions.yaml"))
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    base = args.url.rstrip("/")

    health = json.loads(_open(base + "/api/health", timeout=10).read())
    print("model %s | status %s | knowledge graph %s" % (
        health.get("configured_model"), health.get("status"), health.get("knowledge_graph")))
    if health.get("knowledge_graph") != "ok":
        print("rc-kg is not ready (%s); drafts would be ungrounded. Wait and retry."
              % health.get("knowledge_graph_detail"), file=sys.stderr)
        return 1
    if health.get("status") != "ok":
        print("the model is not reachable yet; wait for vLLM to load.", file=sys.stderr)
        return 1

    token = Path(args.token_file).read_text().strip()
    session = _post(base + "/api/login", {"access_token": token})["session_token"]

    qs = _questions(Path(args.questions))
    if args.limit:
        qs = qs[: args.limit]
    totals = {"drafts": 0, "failed": 0, "with_references": 0, "flagged_caveats": 0}
    for i, q in enumerate(qs, 1):
        t0 = time.time()
        try:
            d = _post(base + "/api/draft", {"ticket_text": q, "ticket_number": "RCKG-EVAL-%02d" % i},
                      token=session)
        except urllib.error.HTTPError as exc:
            totals["failed"] += 1
            print("%2d FAILED HTTP %d  %s" % (i, exc.code, q))
            continue
        totals["drafts"] += 1
        flagged = sum(1 for c in d["caveats"] if c.startswith("Check citation"))
        totals["with_references"] += bool(d["references"])
        totals["flagged_caveats"] += flagged
        print("%2d %4.1fs refs=%d flagged=%d kg=%s  %s" % (
            i, time.time() - t0, len(d["references"]), flagged, d["kg_status"], q))
    print(json.dumps(totals))
    print("next: scripts/with_kg.sh -m eval.citation_review --only RCKG-EVAL")
    return 0 if not totals["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
