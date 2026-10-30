"""Where every trace, metric and evaluation is kept: <repo>/traces/$USER/
(ticketmate/traces/$USER/ or rc-agent/traces/$USER/).

    traces/$USER/
      runs/<ticket>/<time>-<id>.json      the full run log of each draft (+ .feedback.jsonl)
      spans/YYYY-MM-DD.jsonl              OpenTelemetry spans of every draft (also sent to a
                                          collector when OTEL_EXPORTER_OTLP_ENDPOINT is set)
      metrics/YYYY-MM-DD.jsonl            one row per draft: route, tools, reviews,
                                          guardrails, retrieval, latency, tokens, versions
      guardrails/YYYY-MM-DD.jsonl         every guardrail that fired, with the request id
      evals/<time>-<suite>/               evaluation reports (report.json, report.md, rows.jsonl)

On /projects, beside the code, so the team's traces are where the code is
and survive /scratch purges. Tickets carry researchers' data, so the
per-user directory is 0700 and every file 0600; `traces/` itself is
git-ignored. RC_TRACES_DIR moves it; RUN_LOG_DIR still overrides the run
logs alone ("" turns them off). Writing is best-effort: a full disk never
breaks a draft. A member who cannot write the repository (the shared release
belongs to its maintainer) gets /scratch/$USER/rc_copilot_traces instead, with
the same layout and modes, rather than no traces at all.
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

REPO_ROOT = Path(__file__).resolve().parents[3]          # the repository root (ticketmate/ or rc-agent/)


SCRATCH_FALLBACK = Path("/scratch") / os.environ.get("USER", "unknown") / "rc_copilot_traces"
_root: Optional[Path] = None


def root() -> Path:
    """traces/$USER/ in the repository; /scratch when that cannot be created."""
    global _root
    configured = os.environ.get("RC_TRACES_DIR")
    if configured:
        return Path(configured)
    if _root is None:
        repo = REPO_ROOT / "traces" / os.environ.get("USER", "unknown")
        try:
            repo.mkdir(parents=True, exist_ok=True)
            os.chmod(repo, 0o700)
            _root = repo
        except OSError:
            _root = SCRATCH_FALLBACK
    return _root


def path(kind: str) -> Optional[Path]:
    """traces/$USER/<kind>/, created 0700; None if it cannot be."""
    d = root() / kind
    try:
        d.mkdir(parents=True, exist_ok=True)
        for p in (root(), d):
            os.chmod(p, 0o700)
        return d
    except OSError:
        return None


def append(kind: str, row: Dict[str, Any], when: Optional[datetime] = None) -> Optional[Path]:
    """Add one JSON line to traces/$USER/<kind>/<date>.jsonl (0600). Never raises."""
    d = path(kind)
    if d is None:
        return None
    f = d / ("%s.jsonl" % (when or datetime.now()).strftime("%Y-%m-%d"))
    try:
        fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return f
    except OSError:
        return None


def new_eval_dir(suite: str) -> Optional[Path]:
    """traces/$USER/evals/<YYYYmmdd-HHMMSS>-<suite>/ for one evaluation's reports."""
    d = path("evals")
    if d is None:
        return None
    out = d / ("%s-%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), "".join(
        c for c in suite if c.isalnum() or c in "-_")[:40]))
    out.mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    return out
