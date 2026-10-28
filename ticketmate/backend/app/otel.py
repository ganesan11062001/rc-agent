"""Export each draft's run tree as OpenTelemetry spans -- always to
traces/$USER/spans/<date>.jsonl, and to a collector when one is configured.

The run log (runlog.py) keeps the full trace of every draft on /scratch. For
production monitoring the same tree also goes to any OpenTelemetry
collector -- Jaeger, Grafana Tempo, Arize Phoenix, Langfuse, Datadog -- over
OTLP/HTTP, when the standard variables say where:

    OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4318   (or ..._TRACES_ENDPOINT)
    OTEL_SERVICE_NAME=rc-agent                         (default)
    OTEL_EXPORT_CONTENT=true                            ticket/draft text too (off by default)

One trace per draft, one span per run, with the run's real start and end
times and parent. Attributes follow the OpenTelemetry GenAI semantic
conventions where they apply -- `gen_ai.operation.name`,
`gen_ai.request.model`, `gen_ai.usage.input_tokens` / `output_tokens` on
model calls, `gen_ai.tool.name` on tool calls -- plus `rc.*` for the rest:
run type and id, request, thread (ticket), project, route, tags and the
run's scalar metadata. Content (inputs, outputs: ticket text, prompts,
drafts) is left out unless OTEL_EXPORT_CONTENT is set: tickets carry
researchers' data, and a collector is usually shared.

Export happens after the draft is returned and never raises.
"""

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_provider = None
_tracer = None
MAX_CONTENT = 4000


def enabled() -> bool:
    """A collector is configured (the standard OTLP variables)."""
    return bool(os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"))


def local_enabled() -> bool:
    """Spans are also kept in traces/$USER/spans/ (app/ops_store.py) unless RC_TRACES_SPANS=off."""
    return os.environ.get("RC_TRACES_SPANS", "on").lower() not in ("0", "off", "false", "no")


def _file_exporter():
    """An exporter appending each span, as OTLP-style JSON, to traces/$USER/spans/<date>.jsonl."""
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult
    from . import ops_store

    class FileSpanExporter(SpanExporter):
        def export(self, spans):
            for sp in spans:
                ops_store.append("spans", json.loads(sp.to_json()))
            return SpanExportResult.SUCCESS

        def shutdown(self):
            return None

    return FileSpanExporter()


def _content() -> bool:
    return os.environ.get("OTEL_EXPORT_CONTENT", "").lower() in ("1", "true", "yes")


def configure(exporter=None):
    """Set up the tracer once. `exporter` is for tests (an in-memory exporter);
    otherwise OTLP/HTTP from the standard environment variables."""
    global _provider, _tracer
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "rc-agent"),
                                "service.namespace": "research-computing"})
    _provider = TracerProvider(resource=resource)
    if exporter is not None:
        _provider.add_span_processor(SimpleSpanProcessor(exporter))
    else:
        if local_enabled():
            _provider.add_span_processor(SimpleSpanProcessor(_file_exporter()))
        if enabled():
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            _provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    _tracer = _provider.get_tracer("rc-agent.drafts")
    return _provider


def _ns(iso: Optional[str]) -> Optional[int]:
    if not iso:
        return None
    try:
        return int(datetime.fromisoformat(iso).timestamp() * 1e9)
    except ValueError:
        return None


def _scalar(v: Any) -> Any:
    if isinstance(v, (str, bool, int, float)):
        return v if not isinstance(v, str) else v[:500]
    if isinstance(v, (list, tuple)) and all(isinstance(x, (str, int, float, bool)) for x in v):
        return [x if not isinstance(x, str) else x[:200] for x in v][:50]
    return None


def _attributes(run: Dict[str, Any], root_meta: Dict[str, Any]) -> Dict[str, Any]:
    a: Dict[str, Any] = {"rc.run_id": run["id"], "rc.run_type": run["run_type"], "rc.status": run.get("status") or "",
                         "rc.request_id": str(root_meta.get("request_id", "")),
                         "rc.thread_id": str(root_meta.get("thread_id", "")),
                         "rc.project": str(root_meta.get("project", ""))}
    if run.get("tags"):
        a["rc.tags"] = [str(t) for t in run["tags"]][:50]
    for k, v in (run.get("metadata") or {}).items():
        s = _scalar(v)
        if s is not None:
            a["rc.meta." + k] = s
    meta = run.get("metadata") or {}
    if run["run_type"] == "llm":
        a["gen_ai.operation.name"] = "chat"
        a["gen_ai.system"] = str(meta.get("ls_provider") or "vllm")
        if meta.get("ls_model_name"):
            a["gen_ai.request.model"] = str(meta["ls_model_name"])
        usage = run.get("usage") or {}
        if usage.get("prompt_tokens") is not None:
            a["gen_ai.usage.input_tokens"] = int(usage["prompt_tokens"])
        if usage.get("completion_tokens") is not None:
            a["gen_ai.usage.output_tokens"] = int(usage["completion_tokens"])
    if run["run_type"] == "tool" and run["name"].startswith("tool."):
        a["gen_ai.operation.name"] = "execute_tool"
        a["gen_ai.tool.name"] = run["name"][5:]
    if run.get("latency_ms") is not None:
        a["rc.latency_ms"] = float(run["latency_ms"])
    if _content():
        for side in ("inputs", "outputs"):
            if run.get(side):
                a["rc." + side] = json.dumps(run[side], default=str)[:MAX_CONTENT]
    return {k: v for k, v in a.items() if v is not None}


def export(trace: Dict[str, Any]) -> int:
    """Send one trace (tracing.Trace.to_dict()); returns the number of spans. Never raises."""
    if _tracer is None:
        if not (enabled() or local_enabled()):
            return 0
        try:
            configure()
        except Exception as exc:                       # noqa: BLE001
            logger.warning("OpenTelemetry export unavailable: %s", exc)
            return 0
    try:
        from opentelemetry import trace as ot
        from opentelemetry.trace import Status, StatusCode
        runs = trace.get("runs") or []
        root = next((r for r in runs if r.get("parent_id") is None), None)
        root_meta = (root or {}).get("metadata") or {}
        spans: Dict[str, Any] = {}
        for r in runs:                                # creation order: parents before children
            parent = spans.get(r.get("parent_id"))
            ctx = ot.set_span_in_context(parent) if parent is not None else None
            span = _tracer.start_span(r["name"], context=ctx, start_time=_ns(r.get("start_time")),
                                      attributes=_attributes(r, root_meta))
            if r.get("status") == "error":
                span.set_status(Status(StatusCode.ERROR, str(r.get("error") or "")[:300]))
            spans[r["id"]] = span
        for fb in trace.get("feedback") or []:        # scores as events on the run they rate
            target = spans.get(fb.get("run_id")) or (spans.get(root["id"]) if root else None)
            if target is not None and isinstance(fb.get("score"), (int, float)):
                target.add_event("feedback", {"key": str(fb.get("key")), "score": float(fb["score"]),
                                              "source": str(fb.get("source", ""))})
        for r in reversed(runs):                      # children end before their parents
            spans[r["id"]].end(end_time=_ns(r.get("end_time")) or None)
        return len(spans)
    except Exception as exc:                           # noqa: BLE001 -- export never breaks a draft
        logger.warning("OpenTelemetry export failed: %s", exc)
        return 0
