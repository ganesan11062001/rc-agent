"""Record every drafting run to scratch, so a draft can be explained later.

Motivation: a draft that lands in a ticket is otherwise unaccountable. If an RC
member asks "why did it say that?", or a reviewer asks what the model was
actually shown, there has to be a record. This writes one JSON file per request
containing the exact prompt sent, the raw text that came back, what the parsed
draft was, and how long it took.

Storage notes:
  * Default is /scratch/$USER/rc_copilot_runs, which is the right filesystem
    for bulky, regenerable data.
  * /scratch is mode 770 -- group-accessible, unlike $HOME -- so the subtree is
    forced to 0700 and every file to 0600. Ticket text can contain researcher
    PII.
  * Files are grouped per ticket number, so every run against one ticket
    collects in one directory.
  * Failures here never break a request: recording is best-effort.
"""

import json
import os
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# ServiceNow record prefixes, longest first so SCTASK is not matched as TASK.
_TICKET_RE = re.compile(
    r"\b(SCTASK|RITM|INC|TASK|CHG|PRB|REQ|CS)(\d{5,})\b", re.IGNORECASE)


def _default_dir() -> Path:
    return Path("/scratch") / os.environ.get("USER", "unknown") / "rc_copilot_runs"


def run_dir() -> Optional[Path]:
    """The directory runs are written to, or None if recording is disabled."""
    configured = os.environ.get("RUN_LOG_DIR")
    if configured == "":          # explicitly turned off
        return None
    target = Path(configured) if configured else _default_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        return target
    except OSError:
        return None


def ticket_folder(explicit: str, text: str) -> str:
    """Directory name for a ticket. Falls back to scanning the text."""
    candidate = (explicit or "").strip()
    if not candidate:
        found = _TICKET_RE.search(text or "")
        candidate = found.group(0) if found else ""
    if not candidate:
        return "unknown-ticket"
    # The value reaches us from a web page, so never let it escape run_dir().
    safe = re.sub(r"[^A-Za-z0-9_-]", "", candidate).upper()
    return safe[:64] or "unknown-ticket"


def record(
    *,
    request_id: str,
    ticket_number: str,
    ticket_text: str,
    extra_instructions: str,
    messages: List[Dict[str, str]],
    raw_response: str,
    strategy: str,
    draft: Optional[Dict[str, Any]],
    error: Optional[str],
    model: str,
    latency_ms: int,
    trace: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """Write one run. Returns the path written, or None if recording is off.

    `trace` is the full trace of the draft (trace_version 2: retrieval, every
    model call with its tokens, citation processing, timings, versions). The
    older top-level fields stay, so readers of version-1 logs keep working."""
    base = run_dir()
    if base is None:
        return None

    folder = ticket_folder(ticket_number, ticket_text)
    stamp = datetime.now().astimezone()

    try:
        target = base / folder
        target.mkdir(parents=True, exist_ok=True)
        os.chmod(target, 0o700)
        path = target / ("%s-%s.json" % (stamp.strftime("%Y%m%d-%H%M%S"),
                                         request_id or uuid.uuid4().hex[:8]))

        payload = {
            "request_id": request_id,
            "ticket_number": folder,
            "at": stamp.isoformat(timespec="seconds"),
            "node": os.uname().nodename,
            "slurm_job_id": os.environ.get("SLURM_JOB_ID", "n/a"),
            "model": model,
            "latency_ms": latency_ms,
            # Which structured-output strategy actually worked. Useful when a
            # draft looks odd: prompt-only output is more likely to be ragged.
            "strategy": strategy,
            "input": {
                "ticket_text": ticket_text,
                "extra_instructions": extra_instructions,
                "chars": len(ticket_text),
            },
            # The exact prompt, so a draft can be reproduced or argued with.
            "prompt": messages,
            "raw_response": raw_response,
            "draft": draft,
            "error": error,
        }
        if trace is not None:
            payload["trace_version"] = 2
            payload["trace"] = trace

        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        return str(path)
    except OSError:
        return None
