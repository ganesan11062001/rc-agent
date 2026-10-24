"""A stand-in LLM judge for testing eval/ragas_llm.py's plumbing, not quality.

OpenAI-compatible. ragas calls it through instructor's JSON mode, which puts
the expected JSON schema in the prompt; this server finds that schema and
returns an object that satisfies it (strings "ok", numbers 1, the first enum
value, one item per array). Every score it produces is meaningless -- it only
proves that samples reach the judge, verdicts parse, and metrics come back.

  python tests/stub_judge.py 8099
"""

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer


def _schemas(text: str):
    """Top-level JSON schemas in the prompt (not the definitions nested
    inside one, which look like schemas too)."""
    dec = json.JSONDecoder()
    covered = -1
    for m in re.finditer(r"\{", text):
        if m.start() < covered:
            continue
        try:
            obj, length = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        covered = m.start() + length
        # A JSON *schema* declares types for its properties; an example
        # output in the prompt is a plain object and is skipped.
        props = obj.get("properties") if isinstance(obj, dict) else None
        if isinstance(props, dict) and props and all(
                isinstance(v, dict) and ({"type", "$ref", "anyOf", "allOf", "items"} & set(v))
                for v in props.values()):
            yield obj


def _fill(schema, defs):
    if "$ref" in schema:
        return _fill(defs[schema["$ref"].split("/")[-1]], defs)
    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema:
            return _fill(next(s for s in schema[key] if s.get("type") != "null"), defs)
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object" or "properties" in schema:
        return {k: _fill(v, defs) for k, v in schema.get("properties", {}).items()}
    if t == "array":
        return [_fill(schema.get("items", {"type": "string"}), defs)]
    return {"string": "ok", "number": 1.0, "integer": 1, "boolean": True}.get(t, "ok")


class H(BaseHTTPRequestHandler):
    def _send(self, obj):
        out = json.dumps(obj).encode()
        self.send_response(200); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

    def do_GET(self):
        self._send({"object": "list", "data": [{"id": "stub-judge", "object": "model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        text = "\n".join(str(m.get("content", "")) for m in body.get("messages", []))
        schemas = list(_schemas(text))
        answer = _fill(schemas[-1], schemas[-1].get("$defs", {})) if schemas else {}
        self._send({"id": "x", "object": "chat.completion", "model": "stub-judge",
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": json.dumps(answer)}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
