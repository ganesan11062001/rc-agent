"""Stand-in for vLLM: returns a fixed TicketMate-shaped draft.

For the OOM ticket it cites the OOM section correctly; for a "pending" GPU
ticket it cites the Partitions page correctly and a non-Partitions page for
the same claim, which the citation check must flag. It also writes one
invented URL, which must be removed.
"""
import json, os, sys
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
        if self.path.rstrip("/").endswith("/tokenize"):
            # Like vLLM's /tokenize; ~4 characters a token is close enough here.
            return self._send({"count": max(1, len(body.get("prompt", "")) // 4)})
        # STUB_REJECT_SCHEMA=1: refuse json_schema like a server without guided
        # decoding, so the backend's fallback to json_object is exercised.
        if os.environ.get("STUB_REJECT_SCHEMA") and (body.get("response_format") or {}).get("type") == "json_schema":
            out = json.dumps({"object": "error", "message": "response_format json_schema is unsupported",
                              "type": "BadRequestError", "code": 400}).encode()
            self.send_response(400); self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)
            return
        user = body["messages"][1]["content"]
        prompt_tokens = sum(len(m["content"]) for m in body["messages"]) // 4 + 12
        if "strict evaluator" in body["messages"][0]["content"]:
            # The background LLM judge (app/evaluators.py): a fixed, valid verdict.
            out = json.dumps({"hallucination": {"score": 0.1, "unsupported_claims": ["made-up guide"]},
                              "correctness": {"score": 0.9, "reason": "raises --mem as the sources say"},
                              "conciseness": {"score": 0.8, "reason": "short"},
                              "answer_relevance": {"score": 0.95, "reason": "on topic"}})
            return self._send({"id": "j", "object": "chat.completion", "model": "stub-model",
                               "choices": [{"index": 0, "finish_reason": "stop",
                                            "message": {"role": "assistant", "content": out}}],
                               "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(out) // 4,
                                         "total_tokens": prompt_tokens + len(out) // 4}})
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
            out = json.dumps(draft)
            self._send({"id": "x", "object": "chat.completion", "model": "stub-model",
                        "choices": [{"index": 0, "finish_reason": "stop",
                                     "message": {"role": "assistant", "content": out}}],
                        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(out) // 4,
                                  "total_tokens": prompt_tokens + len(out) // 4}})
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
        out = json.dumps(draft)
        self._send({"id": "x", "object": "chat.completion", "model": "stub-model",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": out}}],
                    "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": len(out) // 4,
                              "total_tokens": prompt_tokens + len(out) // 4}})
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
