"""Stand-in for vLLM: returns a fixed TicketMate-shaped draft.

For the OOM ticket it cites the OOM section correctly; for a "pending" GPU
ticket it cites the Partitions page correctly and a non-Partitions page for
the same claim, which the citation check must flag. It also writes one
invented URL, which must be removed.
"""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def _send(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "stub-model", "object": "model"}]})
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        user = body["messages"][1]["content"]
        pass
        # Find which source is the OOM section and which is on the Partitions page, so the
        # stub can cite one correctly and one deliberately wrongly.
        import re
        src = dict(re.findall(r"^\[(S\d+)\] (.+)$", user, re.M))
        oom = next((r for r, t in src.items() if "Out of Memory" in t), "S1")
        wrong = next((r for r, t in src.items() if t.startswith("Partitions")), "S5")
        ticket = user.split("--- BEGIN TICKET TEXT ---", 1)[-1].split("--- END TICKET TEXT ---", 1)[0]
        if "pending" in ticket:
            part = next((r for r, t in src.items() if t.startswith("Partitions")), "S1")
            ood = next((r for r, t in src.items() if t.startswith("Open OnDemand")), "S2")
            draft = {
                "problem_summary": "GPU job pending on the gpu partition [%s]." % part,
                "suggested_steps": ["The gpu partition allows 4 running jobs per user with an 8 hour maximum [%s]" % part,
                                    "The gpu partition allows 4 running jobs per user with an 8 hour maximum [%s]" % ood],
                "confidence": 0.7, "caveats": [],
                "draft_response": "Hi,\n\nThe gpu partition allows 4 running jobs per user with an 8 hour maximum [%s].\n\nBest regards,\nResearch Computing" % part,
            }
            self._send({"id": "x", "object": "chat.completion", "model": "stub-model",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": json.dumps(draft)}}]})
            return
        draft = {
            "problem_summary": "The job exceeded its memory request and was OOM-killed [%s]." % oom,
            "suggested_steps": ["Raise the memory request with --mem and resubmit [%s]" % oom,
                                "Check the job's peak memory usage with seff [%s]" % wrong],
            "confidence": 0.85, "caveats": ["Job ID not confirmed."],
            "draft_response": ("Hi,\n\nYour job ran out of memory and was killed [%s]. You can check its "
                               "peak memory use with seff [%s]; see https://rc.northeastern.edu/made-up-oom-guide/ "
                               "too.\n\nBest regards,\nResearch Computing") % (oom, wrong),
        }
        self._send({"id": "x", "object": "chat.completion", "model": "stub-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": json.dumps(draft)}}]})
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
