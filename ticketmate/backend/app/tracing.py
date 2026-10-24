"""Run trees for every draft -- the LangSmith data model, kept private.

A *trace* is one draft. It is a tree of *runs*, each one step with its own
id, parent, type, timing, status, inputs, outputs, metadata and tags:

  draft                                   chain       root
  ├── rc_kg.retrieve                      retriever   documents + scores
  │   ├── rc_kg.embed_query               embedding   (timings from rc_kg)
  │   ├── rc_kg.vector_search             retriever
  │   ├── rc_kg.fulltext_search           retriever
  │   ├── rc_kg.topic_boost               tool
  │   └── rc_kg.graph_expand              tool
  ├── generate_draft                      chain
  │   ├── llm  (one per call)             llm         messages, params, tokens
  │   └── parse_draft (one per output)    parser
  ├── tokenize.prompt_breakdown           tool
  └── rc_kg.cite                          tool        references, verdicts

Traces are grouped like LangSmith's projects/sessions/threads: `project` (the
deployment, e.g. which graph variant), `session_id` (the Slurm job), and
`thread_id` (the ticket -- every draft for one ticket shares it). Feedback
(people's and automatic) is attached by run id.

Parents are tracked with a context variable, so code deep in the call stack
(llm_client) opens child runs of whatever is running without the trace
being passed down. asyncio tasks and to_thread() inherit it.
"""

import contextvars
import hashlib
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional

RUN_TYPES = ("chain", "retriever", "embedding", "llm", "parser", "tool")

_current: contextvars.ContextVar = contextvars.ContextVar("rc_copilot_run", default=None)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


class Run:
    __slots__ = ("trace", "id", "parent_id", "name", "run_type", "start_time", "end_time",
                 "latency_ms", "status", "error", "inputs", "outputs", "metadata", "tags",
                 "usage", "_t0")

    def __init__(self, trace: "Trace", name: str, run_type: str, parent_id: Optional[str],
                 inputs=None, metadata=None, tags=None):
        assert run_type in RUN_TYPES, run_type
        self.trace, self.name, self.run_type, self.parent_id = trace, name, run_type, parent_id
        self.id = uuid.uuid4().hex
        self.start_time, self._t0 = _now(), time.perf_counter()
        self.end_time = self.latency_ms = self.error = self.usage = None
        self.status = "running"
        self.inputs, self.outputs = dict(inputs or {}), {}
        self.metadata, self.tags = dict(metadata or {}), list(tags or [])

    def end(self, outputs: Optional[Dict] = None, error: Optional[str] = None) -> None:
        if self.end_time is not None:
            return
        self.outputs.update(outputs or {})
        self.end_time = _now()
        self.latency_ms = round((time.perf_counter() - self._t0) * 1000, 1)
        self.status = "error" if error else "success"
        self.error = error

    def to_dict(self) -> Dict[str, Any]:
        return {k: getattr(self, k) for k in self.__slots__ if k not in ("trace", "_t0")}


class Trace:
    def __init__(self, name: str, inputs: Dict, metadata: Dict, tags: List[str], trace_id: str = ""):
        self.id = trace_id or uuid.uuid4().hex
        self.runs: List[Run] = []
        self.feedback: List[Dict[str, Any]] = []
        self.root = self._new(name, "chain", None, inputs, metadata, tags)
        self._token = _current.set(self.root)

    def _new(self, name, run_type, parent_id, inputs=None, metadata=None, tags=None) -> Run:
        r = Run(self, name, run_type, parent_id, inputs, metadata, tags)
        self.runs.append(r)
        return r

    def add_feedback(self, key: str, score: Any, comment: str = "", source: str = "auto",
                     run_id: Optional[str] = None) -> None:
        self.feedback.append({"id": uuid.uuid4().hex, "run_id": run_id or self.root.id, "key": key,
                              "score": score, "comment": comment, "source": source,
                              "created_at": _now()})

    def finish(self, outputs: Optional[Dict] = None, error: Optional[str] = None) -> None:
        for r in self.runs:                       # a run left open by an exception
            if r is not self.root and r.end_time is None:
                r.end(error=error or "did not finish")
        self.root.end(outputs, error)
        try:
            _current.reset(self._token)
        except ValueError:
            pass                                  # finished from another context

    def to_dict(self) -> Dict[str, Any]:
        return {"trace_id": self.id, "root_run_id": self.root.id,
                "runs": [r.to_dict() for r in self.runs], "feedback": self.feedback}


@contextmanager
def run(name: str, run_type: str, inputs: Optional[Dict] = None,
        metadata: Optional[Dict] = None, tags: Optional[List[str]] = None) -> Iterator[Optional[Run]]:
    """Open a child run of whatever run is current. Outside a trace this is a
    no-op that yields None, so instrumented code also works untraced."""
    parent = _current.get()
    if parent is None:
        yield None
        return
    r = parent.trace._new(name, run_type, parent.id, inputs, metadata, tags)
    token = _current.set(r)
    try:
        yield r
    except BaseException as exc:
        r.end(error="%s: %s" % (type(exc).__name__, str(exc)[:300]))
        raise
    finally:
        _current.reset(token)
        if r.end_time is None:
            r.end()


def add_derived(parent: Run, name: str, run_type: str, start_offset_ms: float, duration_ms: float,
                outputs: Optional[Dict] = None, metadata: Optional[Dict] = None) -> Run:
    """A completed child run reconstructed from timings measured elsewhere
    (rc_kg reports its stages' durations; they ran in sequence). Marked as
    derived, since their start times are inferred, not observed here."""
    from datetime import timedelta
    r = parent.trace._new(name, run_type, parent.id, metadata={**(metadata or {}), "derived": True})
    start = datetime.fromisoformat(parent.start_time) + timedelta(milliseconds=start_offset_ms)
    r.start_time = start.isoformat(timespec="milliseconds")
    r.end_time = (start + timedelta(milliseconds=duration_ms)).isoformat(timespec="milliseconds")
    r.latency_ms, r.status, r.outputs = round(duration_ms, 1), "success", dict(outputs or {})
    return r


def prompt_version() -> Dict[str, str]:
    """A content hash of every prompt template, like a prompt-hub commit:
    any wording change gets a new version, so outputs can be compared across
    prompt revisions."""
    from . import prompts
    names = sorted(n for n in dir(prompts) if n.isupper() and isinstance(getattr(prompts, n), str))
    h = hashlib.sha256()
    for n in names:
        h.update(n.encode() + b"\0" + getattr(prompts, n).encode() + b"\0")
    return {"version": h.hexdigest()[:12], "templates": ",".join(names)}
