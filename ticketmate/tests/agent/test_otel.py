"""OpenTelemetry export of a draft's run tree (backend/app/otel.py).

    backend/.venv/bin/python tests/agent/test_otel.py     exit 0 = pass

Builds a run tree the way a draft does (tracing.Trace + nested runs) and
exports it to OpenTelemetry's in-memory exporter: one span per run, the
same parents, real timestamps, GenAI attributes on model and tool calls,
errors as span status, feedback as events, and no ticket text unless asked.
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from app import otel, tracing  # noqa: E402

fails = []


def check(cond, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


def a_draft():
    tr = tracing.Trace("draft", inputs={"ticket_text": "SECRET researcher text"},
                       metadata={"request_id": "r1", "thread_id": "INC1", "project": "rc-agent/full"},
                       tags=["route:incident"])
    with tracing.run("graph", "chain"):
        with tracing.run("agent.incident.step", "llm", inputs={"messages": ["SECRET prompt"]},
                         metadata={"ls_provider": "vllm", "ls_model_name": "qwen2.5-7b"}) as r:
            time.sleep(0.01)
            r.usage = {"prompt_tokens": 120, "completion_tokens": 30, "total_tokens": 150}
            r.end({"content": "SECRET completion"})
        with tracing.run("tool.lookup_node", "tool", inputs={"node": "d4043"}, metadata={"agent": "incident"}) as t:
            t.end({"result": "DOWN"})
        with tracing.run("tool.lookup_job", "tool", inputs={"job_id": "x"}) as t:
            t.end(error="refused: invalid arguments")
    tr.add_feedback("thumbs", 1.0, source="user")
    tr.finish({"draft": {"draft_response": "SECRET reply"}})
    return tr.to_dict()


mem = InMemorySpanExporter()
otel.configure(mem)
d = a_draft()
n = otel.export(d)
spans = {s.name: s for s in mem.get_finished_spans()}
check(n == len(d["runs"]) == len(spans) == 5, "one span per run (%d)" % n)
check(len({s.context.trace_id for s in spans.values()}) == 1, "one trace per draft")
root = spans["draft"]
check(spans["graph"].parent.span_id == root.context.span_id and
      spans["tool.lookup_node"].parent.span_id == spans["graph"].context.span_id, "the run tree's parents are kept")
llm = spans["agent.incident.step"]
check(llm.end_time - llm.start_time >= 9_000_000, "real start and end times (ms-precision timestamps) (%.1f ms)" % ((llm.end_time - llm.start_time) / 1e6))
check(llm.attributes["gen_ai.operation.name"] == "chat" and llm.attributes["gen_ai.request.model"] == "qwen2.5-7b"
      and llm.attributes["gen_ai.usage.input_tokens"] == 120 and llm.attributes["gen_ai.usage.output_tokens"] == 30,
      "model calls carry the GenAI attributes and token counts")
tool = spans["tool.lookup_node"]
check(tool.attributes["gen_ai.tool.name"] == "lookup_node" and tool.attributes["rc.meta.agent"] == "incident",
      "tool calls carry the tool name and the agent")
check(spans["tool.lookup_job"].status.status_code.name == "ERROR", "a refused call is an error span")
check(root.attributes["rc.thread_id"] == "INC1" and root.attributes["rc.project"] == "rc-agent/full"
      and "route:incident" in root.attributes["rc.tags"], "request, ticket, project and tags on every span")
check(any(e.name == "feedback" and e.attributes["score"] == 1.0 for e in root.events), "feedback is an event")
blob = repr([dict(s.attributes) for s in spans.values()])
check("SECRET" not in blob, "no ticket, prompt or draft text by default")

os.environ["OTEL_EXPORT_CONTENT"] = "true"
mem.clear()
otel.export(a_draft())
blob = repr([dict(s.attributes) for s in mem.get_finished_spans()])
check("SECRET prompt" in blob and "SECRET reply" in blob, "with OTEL_EXPORT_CONTENT=true, content is exported too")
del os.environ["OTEL_EXPORT_CONTENT"]

otel._tracer = None
os.environ.pop("OTEL_EXPORTER_OTLP_ENDPOINT", None)
os.environ["RC_TRACES_SPANS"] = "off"
check(otel.export(a_draft()) == 0, "no collector and local spans off: nothing exported, nothing raised")

import tempfile, json as _json  # noqa: E401,E402
os.environ["RC_TRACES_DIR"] = tempfile.mkdtemp()
os.environ["RC_TRACES_SPANS"] = "on"
otel._tracer = None
n = otel.export(a_draft())
files = list(Path(os.environ["RC_TRACES_DIR"], "spans").glob("*.jsonl"))
lines = [_json.loads(l) for f in files for l in f.read_text().splitlines()]
check(n == 5 and len(lines) == 5 and {l["name"] for l in lines} >= {"draft", "tool.lookup_node"},
      "by default every span is also kept in traces/$USER/spans/ (%d lines)" % len(lines))
check(oct(os.stat(files[0]).st_mode & 0o777) == "0o600", "span files are 0600")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
