"""The agent graph: an orchestrator, two specialists, a writer and two reviewers.

    START -> orchestrator --+-- incident agent --+
                            +-- request agent ---+--> write --> review --+--> END
                            +-- (follow-up) -----+       ^               |
                                                         +--(problems)---+  at most AGENT_MAX_REVISIONS

- orchestrator: routes the ticket. Rules first, because they are cheap and
  certain: the engineer asked for a follow-up or closing -> "followup" (no
  investigation); a request skill's triggers matched, or a TASK/RITM ticket
  -> "request". Otherwise the model chooses incident or request (guided
  JSON), and "incident" if it cannot.
- incident / request: the specialists, each a ReAct subgraph (react.py)
  with its own prompt and its own tools, starting from the evidence the
  rules already gathered. They only add to it.
- write: TicketMate's writer -- one model call, JSON draft -- passed in by
  the caller, so the agents and the pipeline share one writer.
- review: the rule-based critic (critic.py) and the reviewer agent
  (reviewer.py); their findings send the draft back to `write` once.

The final state is the draft's provenance: the route and why, the plan,
every tool call, the reflections, both reviews and the path through the
graph, with the versions of every prompt, skill and tool used.
"""

import hashlib
import json
import logging
import re
import time
from typing import Any, Awaitable, Callable, Dict, Tuple

from langgraph.graph import END, START, StateGraph

from .. import guardrails, llm_client, lookups, playbooks as playbooks_mod, tracing
from ..llm_client import extract_json
from ..prompts import instruction_wants_sources
from . import critic, prompt_registry as prompts, react, reviewer, skills, tools as tools_mod
from .state import Evidence, GraphState, Review

logger = logging.getLogger(__name__)

Writer = Callable[[Evidence, str, str], Awaitable[Tuple[Dict[str, Any], str]]]
ROUTE_SCHEMA = {"type": "object", "properties": {"route": {"type": "string", "enum": ["incident", "request"]},
                                                 "reason": {"type": "string"}},
                "required": ["route", "reason"], "additionalProperties": False}


def manifest() -> Dict[str, Any]:
    """Versions of everything a draft depends on, for its trace."""
    def h(obj: Any) -> str:
        return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:8]
    return {"prompts": prompts.versions(),
            "skills": {s.name: h(s.to_dict()) for s in skills.catalog()},
            "tools": {t.name: h(t.schema()) for t in tools_mod.registry()}}


async def _route(s: GraphState) -> Dict[str, Any]:
    instr = s.get("instructions") or ""
    ev: Evidence = s["evidence"]
    with tracing.run("graph.orchestrator", "chain", inputs={"instructions": instr[:200]}) as run:
        route, reason = None, ""
        if instr.strip() and (any(m.playbook.on == "instructions" for m in ev.skills)
                              or not instruction_wants_sources(instr)):
            route, reason = "followup", "rules: the engineer asked for a follow-up or closing message"
        elif any(m.playbook.on == "researcher" for m in ev.skills):
            # The skill says which agent it belongs to (playbook header `route:`).
            first = next(m.playbook for m in ev.skills if m.playbook.on == "researcher")
            route, reason = first.route, "rules: skill %s triggered (a %s)" % (first.name, first.route)
        elif re.match(r"(?i)(TASK|RITM|REQ)\d", s.get("ticket_number") or "") or \
                re.search(r"(?m)^Ticket:\s*(TASK|RITM|REQ)\d", s["ticket"]):
            route, reason = "request", "rules: a ServiceNow request (TASK/RITM)"
        usage = []
        if route is None:
            try:
                out = await llm_client.chat(
                    [{"role": "system", "content": prompts.get("orchestrator").text},
                     {"role": "user", "content": s["query"][:3000] or s["ticket"][:3000]}],
                    None, max_tokens=80, name="agent.orchestrator", json_schema=ROUTE_SCHEMA)
                usage = [out.get("usage") or {}]
                data = extract_json(out["content"])
                if data.get("route") in ("incident", "request"):
                    route, reason = data["route"], "model: " + str(data.get("reason", ""))[:200]
            except Exception as exc:                    # noqa: BLE001
                reason = "model unavailable (%s); " % str(getattr(exc, "message", exc))[:80]
            if route is None:
                route, reason = "incident", reason + "default: incident"
        if run is not None:
            run.end({"route": route, "reason": reason})
    return {"route": route, "route_reason": reason, "usage": usage, "path": ["orchestrator"]}


def _specialist(agent: str, max_steps: int, max_tool_calls: int, timeout: float):
    async def node(s: GraphState) -> Dict[str, Any]:
        ev: Evidence = s["evidence"]
        ctx = tools_mod.ToolContext(ticket=s["ticket"], evidence=ev, request_id=s.get("request_id", ""),
                                    timeout=timeout)
        instr = s.get("instructions") or ""
        user = prompts.get("react_user").render(
            ticket=s["ticket"][:6000],
            instructions=("--- INSTRUCTIONS FROM THE RC ENGINEER ---\n%s\n--- END INSTRUCTIONS ---\n" % instr.strip())
            if instr.strip() else "",
            gathered=react.gathered_text(ctx))
        final = await react.run(agent, prompts.get(agent).text, user, s.get("query") or s["ticket"], ctx,
                                max_steps, max_tool_calls)
        return {"evidence": ctx.evidence, "plan": final.get("plan", ""),
                "tool_calls": final.get("records") or [], "reflections": final.get("reflections") or [],
                "agent_stopped": final.get("stopped") or "done", "agent_steps": final.get("steps", 0),
                "usage": final.get("usage") or [], "path": ["%s_agent" % agent] +
                ["%s.%s" % (agent, p) for p in final.get("path") or []]}
    return node


def build(writer: Writer, max_steps: int = 4, max_tool_calls: int = 8, max_revisions: int = 1,
          timeout: float = 5.0, reviewer_on: bool = True):
    """The compiled graph. `writer(evidence, revision_notes, route)` drafts."""

    async def write(s: GraphState) -> Dict[str, Any]:
        notes = ""
        last = [r for r in s.get("reviews") or [] if not r.ok]
        if last and s.get("draft"):
            notes = "\n".join("- " + p for r in last[-3:] for p in r.problems)
        with tracing.run("graph.write", "chain", inputs={"revision": s.get("revisions", 0), "notes": notes}) as run:
            draft, model = await writer(s["evidence"], notes, s.get("route", "incident"))
            if run is not None:
                run.end({"draft": draft})
        return {"draft": draft, "model": model, "revisions": s.get("revisions", 0) + (1 if notes else 0),
                "path": ["write" if not notes else "revise"]}

    async def review(s: GraphState) -> Dict[str, Any]:
        ev: Evidence = s["evidence"]
        closing = s.get("route") == "followup"
        missing = [a for m in ev.skills for a in m.missing]
        with tracing.run("graph.review", "chain", inputs={"revision": s.get("revisions", 0)}) as run:
            rules = Review("critic", critic.review(s["draft"], s["ticket"], lookups.prompt_text(ev.lookups),
                                                   missing, closing=closing))
            _, gfind = guardrails.check_output(s["draft"], s["ticket"],
                                               playbooks_mod.prompt_text(ev.skills) + "\n" + lookups.prompt_text(ev.lookups))
            found = [rules, Review("guardrails", guardrails.revision_notes(gfind),
                                   detail="; ".join(f.rule for f in gfind))]
            if reviewer_on and not closing:
                found.append(await reviewer.review(s["draft"], s["ticket"], ev))
            if run is not None:
                run.end({"reviews": [r.to_dict() for r in found]})
        return {"reviews": found, "path": ["review"]}

    def after_route(s: GraphState) -> str:
        return {"incident": "incident", "request": "request"}.get(s["route"], "write")

    def after_review(s: GraphState) -> str:
        latest = [r for r in (s.get("reviews") or [])[-3:]]
        bad = any(not r.ok for r in latest)
        return "write" if bad and s.get("revisions", 0) < max_revisions else END

    g = StateGraph(GraphState)
    g.add_node("orchestrator", _route)
    g.add_node("incident", _specialist(tools_mod.INCIDENT, max_steps, max_tool_calls, timeout))
    g.add_node("request", _specialist(tools_mod.REQUEST, max_steps, max_tool_calls, timeout))
    g.add_node("write", write)
    g.add_node("review", review)
    g.add_edge(START, "orchestrator")
    g.add_conditional_edges("orchestrator", after_route, ["incident", "request", "write"])
    g.add_edge("incident", "write")
    g.add_edge("request", "write")
    g.add_edge("write", "review")
    g.add_conditional_edges("review", after_review, ["write", END])
    return g.compile()


async def run(writer: Writer, *, request_id: str, ticket: str, query: str, instructions: str,
              ticket_number: str, evidence: Evidence, max_steps: int = 4, max_tool_calls: int = 8,
              max_revisions: int = 1, timeout: float = 5.0, reviewer_on: bool = True) -> GraphState:
    """Run the whole graph for one draft. Writer errors (LLMError) propagate,
    as in the pipeline; everything else degrades to fewer steps."""
    graph = build(writer, max_steps, max_tool_calls, max_revisions, timeout, reviewer_on)
    init: GraphState = {"request_id": request_id, "ticket": ticket, "query": query,
                        "instructions": instructions or "", "ticket_number": ticket_number or "",
                        "evidence": evidence, "revisions": 0}
    t0 = time.perf_counter()
    with tracing.run("graph", "chain", inputs={"seed": evidence.summary()},
                     metadata={"manifest": manifest(), "max_steps": max_steps, "max_tool_calls": max_tool_calls,
                               "max_revisions": max_revisions}) as run_:
        final = await graph.ainvoke(init, config={"recursion_limit": 40})
        final["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        if run_ is not None:
            run_.end({"route": final.get("route"), "path": final.get("path"),
                      "reviews": [r.to_dict() for r in final.get("reviews") or []]})
    return final


def provenance(final: GraphState) -> Dict[str, Any]:
    """The run log's `trace.agent`: how this draft was produced, step by step."""
    usage: Dict[str, int] = {}
    for u in final.get("usage") or []:
        for k, v in (u or {}).items():
            if isinstance(v, int):
                usage[k] = usage.get(k, 0) + v
    return {"route": final.get("route"), "route_reason": final.get("route_reason"),
            "plan": final.get("plan"), "stopped": final.get("agent_stopped"), "steps": final.get("agent_steps"),
            "tool_calls": [r.to_dict() for r in final.get("tool_calls") or []],
            "reflections": final.get("reflections") or [],
            "reviews": [r.to_dict() for r in final.get("reviews") or []],
            "revisions": final.get("revisions", 0), "path": final.get("path") or [],
            "evidence": final["evidence"].summary() if final.get("evidence") else None,
            "usage": usage, "latency_ms": final.get("latency_ms"), "manifest": manifest()}
