"""Capture-only backend: stores what the extension sends, runs no model.

Purpose: prove the data path end to end -- ServiceNow DOM -> content script ->
service worker -> OOD proxy -> Slurm job -> disk -- without waiting for a GPU
or involving an LLM.

It deliberately speaks the *same* API as the real backend (/api/health,
/api/login, /api/draft) and reuses rc-copilot's actual auth module, so this
exercises the real authentication and the real request shape. The extension
needs no changes and no special mode.

Instead of drafting, /api/draft writes the ticket text to a file and returns a
draft that echoes back what it received, so you can confirm in ServiceNow that
the round trip worked and read exactly what was scraped.

Captures go to $HOME (mode 600), never to group-readable /projects.
"""

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# Reuse the product's auth rather than reimplementing it: the point is to test
# the real thing.
BACKEND_ROOT = Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND_ROOT))

from fastapi import Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

from app import auth  # noqa: E402

# Default to scratch: captures are bulky and disposable, and scratch is the
# right filesystem for that. NOTE scratch is mode 770 -- group-accessible -- so
# our own subtree is locked to 0700 and every file to 0600, because ticket text
# can contain researcher PII.
_DEFAULT = Path("/scratch") / os.environ.get("USER", "unknown") / "rc_copilot_captures"
CAPTURE_DIR = Path(os.environ.get("CAPTURE_DIR", _DEFAULT))
try:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CAPTURE_DIR, 0o700)
except OSError as exc:      # scratch unavailable on this node -> fall back
    CAPTURE_DIR = Path.home() / "rc_copilot_captures"
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(CAPTURE_DIR, 0o700)
    print("warning: could not use scratch (%s); using %s" % (exc, CAPTURE_DIR), flush=True)

# ServiceNow record prefixes. Longest first so SCTASK isn't matched as TASK.
TICKET_RE = re.compile(
    r"\b(SCTASK|RITM|INC|TASK|CHG|PRB|REQ|CS)(\d{5,})\b", re.IGNORECASE)


def ticket_folder(explicit: str, text: str) -> str:
    """Directory name for this ticket. Falls back to a scan of the text."""
    candidate = (explicit or "").strip()
    if not candidate:
        found = TICKET_RE.search(text or "")
        candidate = found.group(0) if found else ""
    if not candidate:
        return "unknown-ticket"
    # Never let a page-supplied value escape CAPTURE_DIR.
    safe = re.sub(r"[^A-Za-z0-9_-]", "", candidate).upper()
    return safe[:64] or "unknown-ticket"

app = FastAPI(title="RC Copilot — capture only", version="0.1.0")


class DraftRequest(BaseModel):
    ticket_text: str = Field(..., min_length=1, max_length=200_000)
    ticket_number: str = Field(default="", max_length=64)
    extra_instructions: str = Field(default="", max_length=10_000)


@app.exception_handler(HTTPException)
async def _http_error(request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code,
                        content={"error": exc.detail, "detail": None})


@app.get("/api/health")
async def health():
    """Same shape the extension expects, so Test Connection works unchanged."""
    return {
        "status": "ok",
        "auth_required": True,
        "vllm_base_url": "n/a (capture mode)",
        "configured_model": "capture-only",
        "model_available": True,
        "available_models": ["capture-only"],
        "detail": "No model is running. Requests are stored, not answered.",
    }


@app.post("/api/login")
async def login(body: dict):
    token = (body or {}).get("access_token", "")
    if not auth.check_access_token(token):
        raise HTTPException(status_code=401, detail="That access token isn't right.")
    session_token, expires_at = auth.mint_session()
    return {
        "session_token": session_token,
        "expires_at": expires_at,
        "session_hours": auth.get_settings().session_hours,
    }


@app.post("/api/draft", dependencies=[Depends(auth.require_session)])
async def draft(request: DraftRequest):
    started = time.perf_counter()
    text = request.ticket_text

    capture_id = uuid.uuid4().hex[:8]
    stamp = datetime.now().astimezone()

    # One directory per ticket, so repeat captures of the same ticket collect
    # together and are trivial to find by number.
    folder = ticket_folder(request.ticket_number, text)
    target_dir = CAPTURE_DIR / folder
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)
    path = target_dir / ("%s-%s.json" % (stamp.strftime("%Y%m%d-%H%M%S"), capture_id))

    record = {
        "id": capture_id,
        "ticket_number": folder,
        "received_at": stamp.isoformat(timespec="seconds"),
        "node": os.uname().nodename,
        "slurm_job_id": os.environ.get("SLURM_JOB_ID", "n/a"),
        "chars": len(text),
        "lines": text.count("\n") + 1,
        "extra_instructions": request.extra_instructions,
        "ticket_text": text,
    }

    # 0600: ticket text may contain researcher PII.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2, ensure_ascii=False)

    # Log metadata only -- never the ticket body.
    print("captured | id=%s ticket=%s chars=%d lines=%d instructions=%d -> %s"
          % (capture_id, folder, record["chars"], record["lines"],
             len(request.extra_instructions), path.relative_to(CAPTURE_DIR)), flush=True)

    preview = text if len(text) <= 1500 else text[:1500] + "\n...[truncated]"
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    # Shaped exactly like a real DraftResponse so content.js renders it and
    # writes it into the ticket -- that is what proves the return leg works.
    return {
        "problem_summary": (
            "CAPTURE TEST: the backend received %d characters from this ticket "
            "and stored them on %s." % (record["chars"], record["node"])
        ),
        "suggested_steps": [
            "Data path works: ServiceNow -> extension -> OOD -> Slurm job -> disk.",
            "Ticket identified as: %s" % folder,
            "Read it back with: cat %s" % path,
        ],
        "confidence": 1.0,
        "caveats": [
            "No model was involved. This text is an echo, not a drafted reply.",
            "Check the captured text below matches what the ticket actually says.",
        ] + ([
            "Your instructions were received (%d chars) and stored."
            % len(request.extra_instructions)
        ] if request.extra_instructions else []) + [
        ],
        "draft_response": (
            "=== RC COPILOT CAPTURE TEST - DO NOT SEND ===\n"
            "Received %d characters on %s (job %s) at %s.\n"
            "Ticket: %s\nStored as: %s\n\n"
            "----- exactly what the extension scraped from this page -----\n%s\n"
            "----- end -----\n"
            % (record["chars"], record["node"], record["slurm_job_id"],
               stamp.strftime("%Y-%m-%d %H:%M:%S"), folder, path, preview)
        ),
        "confidence_label": "high",
        "model": "capture-only",
        "latency_ms": elapsed_ms,
    }


@app.get("/api/captures")
async def captures(_: None = Depends(auth.require_session)):
    """Metadata for what has been captured so far. No ticket text."""
    out = []
    for f in sorted(CAPTURE_DIR.glob("*/*.json"), reverse=True)[:50]:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            out.append({k: d.get(k) for k in
                        ("id", "ticket_number", "received_at", "chars", "lines")}
                       | {"file": str(f.relative_to(CAPTURE_DIR))})
        except Exception:
            continue
    return {"count": len(out), "dir": str(CAPTURE_DIR), "captures": out}
