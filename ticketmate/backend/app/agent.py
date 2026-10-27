"""The agent: the model decides what else to look up before the reply is written.

TicketMate's fixed pipeline gathers facts by rule -- regexes pick the
lookups, retrieval searches the researcher's words, patterns pick the
playbook -- and one model call writes the draft. rc-agent keeps all of that
and adds one step in between: the model reads the ticket and what the rules
gathered, and calls read-only tools (app/tools.py) for anything else the
reply needs -- the node a ticket only names in passing, a search in its own
words when the sources miss the question, the playbook the patterns did not
pick. Then the same writer as before drafts from everything gathered.

So the agent can only add: if the model calls no tool, or the server does
not support tool calls, or anything fails, the draft is exactly the
pipeline's. Every step and tool call is a run in the draft's trace.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import kg_client, llm_client, lookups, playbooks, tools, tracing
from .lookups import Lookup
from .playbooks import Match
from .prompts import AGENT_SYSTEM_PROMPT, AGENT_USER_TEMPLATE

logger = logging.getLogger(__name__)

AGENT_TICKET_CHARS = 6000      # the ticket as the agent reads it (the writer gets its own fitted copy)


@dataclass
class AgentResult:
    """What the agent gathered (the pipeline's seed plus its own finds) and how it went."""
    lookups: List[Lookup]
    kg: Optional[kg_client.KGContext]
    playbooks: List[Match]
    stopped: str                                   # "done" | "step budget" | "call budget" | "tools unsupported" | "error"
    steps: int = 0
    calls: List[Dict[str, Any]] = field(default_factory=list)
    notes: str = ""                                # the agent's last words (trace only; never in the prompt)
    detail: str = ""
    latency_ms: int = 0
    usage: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"stopped": self.stopped, "steps": self.steps, "tool_calls": self.calls,
                "notes": self.notes, "detail": self.detail, "latency_ms": self.latency_ms,
                "usage": self.usage}


def gathered_text(ctx: tools.ToolContext) -> str:
    """What the agent is told has already been gathered."""
    parts = []
    if ctx.lookups:
        parts.append("Cluster lookups:\n" + "\n".join("- %s -> %s" % (lk.command, lk.summary) for lk in ctx.lookups))
    if ctx.kg is not None and ctx.kg.sources:
        parts.append("RC website / docs sources%s:\n" % (" (weak match)" if ctx.kg.low_confidence else "") +
                     "\n".join("- %s %s > %s" % (s.get("ref"), s.get("page_title", ""), s.get("heading", ""))
                               for s in ctx.kg.sources))
    if ctx.playbooks:
        parts.append("RC procedures (playbooks) loaded:\n" + "\n".join(
            "- %s%s" % (m.playbook.name, ("; missing: " + "; ".join(m.missing)) if m.missing else "")
            for m in ctx.playbooks))
    return "\n\n".join(parts) or "(nothing yet)"


def _add_usage(total: Dict[str, int], usage: Optional[Dict[str, int]]) -> None:
    for k, v in (usage or {}).items():
        if isinstance(v, int):
            total[k] = total.get(k, 0) + v


async def gather(ticket_text: str, instructions: str, seed_lookups: List[Lookup],
                 seed_kg: Optional[kg_client.KGContext], seed_playbooks: List[Match],
                 request_id: str = "", max_steps: int = 4, max_tool_calls: int = 8,
                 timeout: float = 5.0) -> AgentResult:
    """Run the agent loop from the pipeline's results; never raises."""
    t0 = time.perf_counter()
    ctx = tools.ToolContext(ticket_text=ticket_text, request_id=request_id, timeout=timeout,
                            lookups=list(seed_lookups),
                            kg=seed_kg if seed_kg is not None and seed_kg.status == "ok" else None,
                            playbooks=list(seed_playbooks))
    registry = tools.by_name()
    schemas = [t.schema() for t in registry.values()]
    clipped = ticket_text if len(ticket_text) <= AGENT_TICKET_CHARS else \
        ticket_text[:AGENT_TICKET_CHARS] + "\n[... rest of the ticket left out ...]"
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": AGENT_USER_TEMPLATE.format(
            ticket=clipped,
            instructions=("--- INSTRUCTIONS FROM THE RC ENGINEER ---\n%s\n--- END INSTRUCTIONS ---\n"
                          % instructions.strip()) if (instructions or "").strip() else "",
            gathered=gathered_text(ctx))},
    ]
    usage: Dict[str, int] = {}
    stopped, steps, notes, detail = "step budget", 0, "", ""
    with tracing.run("agent", "chain", inputs={"seed": gathered_text(ctx)},
                     metadata={"max_steps": max_steps, "max_tool_calls": max_tool_calls,
                               "tools": list(registry)}) as arun:
        try:
            for steps in range(1, max_steps + 1):
                out = await llm_client.chat(messages, schemas, name="agent.step")
                _add_usage(usage, out.get("usage"))
                budget = max_tool_calls - len(ctx.calls)
                calls = out["tool_calls"][:max(0, budget)]
                if not calls:
                    stopped, notes = ("done" if not out["tool_calls"] else "call budget"), out["content"].strip()
                    break
                messages.append({"role": "assistant", "content": out["content"] or "",
                                 "tool_calls": [{"id": c["id"], "type": "function",
                                                 "function": {"name": c["name"],
                                                              "arguments": json.dumps(c["arguments"])}}
                                                for c in calls]})
                for c in calls:
                    with tracing.run("tool." + c["name"], "tool", inputs=c["arguments"],
                                     metadata={"read_only": True, "step": steps}) as trun:
                        text, ok = await tools.call(c["name"], c["arguments"], ctx, registry)
                        if trun is not None:
                            trun.end({"result": text}, error=None if ok else text[:200])
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": text})
                if len(ctx.calls) >= max_tool_calls:
                    stopped = "call budget"
                    break
        except llm_client.ToolsUnsupported as exc:
            stopped, detail = "tools unsupported", exc.detail or exc.message
            logger.warning("agent: model server has no tool calls, drafting from the pipeline only")
        except Exception as exc:                     # noqa: BLE001 -- the agent never breaks a draft
            stopped, detail = "error", str(getattr(exc, "message", exc))[:300]
            logger.warning("agent failed, drafting from what was gathered: %s", detail)
        result = AgentResult(lookups=ctx.lookups, kg=ctx.kg if ctx.kg is not None else seed_kg,
                             playbooks=ctx.playbooks, stopped=stopped, steps=steps, calls=ctx.calls,
                             notes=notes[:500], detail=detail,
                             latency_ms=int((time.perf_counter() - t0) * 1000), usage=usage)
        if arun is not None:
            arun.usage = usage or None
            arun.end(result.to_dict(), error=detail if stopped == "error" else None)
    return result
