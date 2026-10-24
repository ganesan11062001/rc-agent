"""An rc_kg that claims API v2.0, to prove TicketMate refuses to guess."""
import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        out = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)
    def do_GET(self):
        self._send({"status": "ok", "api_version": "2.0"})
    def do_POST(self):
        self._send({"renamed": "everything"})
    def log_message(self, *a): pass
HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
