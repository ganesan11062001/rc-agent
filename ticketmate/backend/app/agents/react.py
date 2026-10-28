"""The specialists' reasoning loop: ReAct with a plan and a reflection, as a
LangGraph subgraph.

    START -> plan --(nothing needed)--> END
               |
               v
             reason <--------+          reason: the model thinks, then calls tools
               |  \\          |          act:    the calls are validated and run;
   (no calls)  |   (calls)   |                  their results are the observations
               |    \\-> act -+
               v
            reflect --(gaps, once)--> reason
               |
               v
              END

- plan: one turn without tools -- what the reply still needs and which tool
  gets each. It keeps a 7B model from calling tools at random.
- reason/act: Thought -> Action -> Observation. Several independent calls in
  one turn run concurrently.
- reflect: before stopping, a deterministic check of what is obviously
  still missing -- a job, node or user the researcher named that nobody
  looked up, no skill loaded for a request, no documentation searched for an
  incident -- is put to the model once.

Budgets: `max_steps` model turns after the plan, `max_tool_calls` calls in
all; a repeated call is answered from cache and does not count. If the
server has no tool calls, or anything fails, the loop stops and the
evidence gathered so far stands. Every node is a `react.<agent>.<node>` run
in the trace.
"""

import asyncio
import json
import logging
import operator
from typing import Annotated, Any, Dict, List, TypedDict

from langgraph.graph import END, START, StateGraph

from .. import llm_client, lookups, tracing
from . import prompt_registry as prompts
from . import tools as tools_mod
from .state import ToolCallRecord

logger = logging.getLogger(__name__)


class ReactState(TypedDict, total=False):
    agent: str
    query: str
    messages: List[Dict[str, Any]]
    ctx: tools_mod.ToolContext
    plan: str
    steps: int
    calls: int
    pending: List[Dict[str, Any]]
    reflected: bool
    stopped: str
    notes: str
    records: Annotated[List[ToolCallRecord], operator.add]
    reflections: Annotated[List[str], operator.add]
    usage: Annotated[List[Dict[str, int]], operator.add]
    path: Annotated[List[str], operator.add]


def gathered_text(ctx: tools_mod.ToolContext) -> str:
    """The evidence so far, as the agent is shown it."""
    ev, parts = ctx.evidence, []
    if ev.lookups:
        parts.append("Cluster lookups:\n" + "\n".join("- %s -> %s" % (lk.command, lk.summary) for lk in ev.lookups))
    if ev.kg is not None and ev.kg.status == "ok" and ev.kg.sources:
        parts.append("Knowledge graph sources%s:\n" % (" (weak match)" if ev.kg.low_confidence else "") +
                     "\n".join("- %s %s > %s" % (s.get("ref"), s.get("page_title", ""), s.get("heading", ""))
                               for s in ev.kg.sources))
    if ev.skills:
        parts.append("Skills loaded:\n" + "\n".join(
            "- %s%s" % (m.playbook.name, ("; missing: " + "; ".join(m.missing)) if m.missing else "")
            for m in ev.skills))
    return "\n\n".join(parts) or "(nothing yet)"


def gaps(agent: str, query: str, ctx: tools_mod.ToolContext, tools: Dict[str, tools_mod.Tool],
         searched: bool) -> List[str]:
    """What is obviously still missing, by rule. Only things a tool of this agent can get."""
    ev, out = ctx.evidence, []
    have = {(lk.kind, lk.target) for lk in ev.lookups}
    c = lookups.find_candidates(query)
    for kind, tool, names in (("job", "lookup_job", c.jobs), ("node", "lookup_node", c.nodes),
                              ("user", "lookup_user", c.users), ("partition", "lookup_partition", c.partitions)):
        for n in names:
            if tool in tools and (kind, n) not in have:
                out.append("- the researcher names %s %s, not looked up yet (%s)" % (kind, n, tool))
    if agent == tools_mod.REQUEST and not ev.skills and "load_skill" in tools:
        out.append("- no skill loaded: load the one for this kind of request (load_skill), or say none applies")
    no_sources = ev.kg is None or ev.kg.status != "ok" or not ev.kg.sources or ev.kg.low_confidence
    if agent == tools_mod.INCIDENT and no_sources and not searched and "search_knowledge_graph" in tools:
        out.append("- the documentation found does not cover the problem: search the knowledge graph in "
                   "your own words (search_knowledge_graph)")
    return out


def build(agent: str, max_steps: int = 4, max_tool_calls: int = 8):
    """The compiled ReAct subgraph for one specialist."""
    tools = tools_mod.for_agent(agent)
    schemas = [t.schema() for t in tools.values()]

    async def plan(s: ReactState) -> Dict[str, Any]:
        with tracing.run("react.%s.plan" % agent, "chain") as run:
            msgs = s["messages"] + [{"role": "user", "content": prompts.get("react_plan").text}]
            try:
                out = await llm_client.chat(msgs, None, max_tokens=200, name="agent.%s.plan" % agent)
            except Exception as exc:                     # noqa: BLE001
                if run is not None:
                    run.end(error=str(exc)[:200])
                return {"stopped": "error", "notes": str(getattr(exc, "message", exc))[:300], "path": ["plan"]}
            text = (out["content"] or "").strip()[:800]
            if run is not None:
                run.end({"plan": text})
            done = "nothing more needed" in text.lower() and not gaps(agent, s["query"], s["ctx"], tools, False)
            return {"plan": text, "usage": [out.get("usage") or {}], "path": ["plan"],
                    "stopped": "done" if done else "",
                    "messages": s["messages"] + [{"role": "user", "content":
                                                  prompts.get("react_act").render(plan=text or "(none)")}]}

    async def reason(s: ReactState) -> Dict[str, Any]:
        step = s.get("steps", 0) + 1
        try:
            out = await llm_client.chat(s["messages"], schemas, name="agent.%s.step" % agent)
        except llm_client.ToolsUnsupported as exc:
            return {"stopped": "tools unsupported", "notes": exc.detail or exc.message, "steps": step,
                    "pending": [], "path": ["reason"]}
        except Exception as exc:                         # noqa: BLE001
            return {"stopped": "error", "notes": str(getattr(exc, "message", exc))[:300], "steps": step,
                    "pending": [], "path": ["reason"]}
        budget = max_tool_calls - s.get("calls", 0)
        calls = out["tool_calls"][:max(0, budget)]
        upd: Dict[str, Any] = {"steps": step, "pending": calls, "usage": [out.get("usage") or {}],
                               "path": ["reason"]}
        if calls:
            upd["messages"] = s["messages"] + [{"role": "assistant", "content": out["content"] or "", "tool_calls": [
                {"id": c["id"], "type": "function",
                 "function": {"name": c["name"], "arguments": json.dumps(c["arguments"])}}
                for c in calls]}]
        else:
            upd["notes"] = (out["content"] or "").strip()[:500]
            if out["tool_calls"]:
                upd["stopped"] = "call budget"
        return upd

    async def act(s: ReactState) -> Dict[str, Any]:
        calls, step = s.get("pending") or [], s.get("steps", 0)
        results = await asyncio.gather(*[tools_mod.call(agent, step, c["name"], c["arguments"], s["ctx"], tools)
                                         for c in calls])
        msgs = list(s["messages"]) + [{"role": "tool", "tool_call_id": c["id"], "content": text}
                                      for c, (text, _) in zip(calls, results)]
        recs = [r for _, r in results]
        return {"messages": msgs, "pending": [], "records": recs, "path": ["act"],
                "calls": s.get("calls", 0) + sum(1 for r in recs if not r.cached)}

    async def reflect(s: ReactState) -> Dict[str, Any]:
        searched = any(r.tool == "search_knowledge_graph" for r in s.get("records") or [])
        found = gaps(agent, s["query"], s["ctx"], tools, searched)
        with tracing.run("react.%s.reflect" % agent, "chain", inputs={"gaps": found}) as run:
            if run is not None:
                run.end({"gaps": found, "acted_on": bool(found) and not s.get("reflected")})
        if found and not s.get("reflected") and s.get("steps", 0) < max_steps and s.get("calls", 0) < max_tool_calls:
            msg = prompts.get("react_reflect").render(gaps="\n".join(found))
            return {"reflected": True, "reflections": found, "path": ["reflect"],
                    "messages": s["messages"] + [{"role": "user", "content": msg}]}
        return {"stopped": s.get("stopped") or "done", "path": ["reflect"]}

    def after_plan(s: ReactState) -> str:
        return END if s.get("stopped") else "reason"

    def after_reason(s: ReactState) -> str:
        if s.get("stopped") in ("tools unsupported", "error"):
            return END
        if s.get("pending"):
            return "act"
        return "reflect"

    def after_act(s: ReactState) -> str:
        if s.get("calls", 0) >= max_tool_calls:
            return "reflect"
        return "reason" if s.get("steps", 0) < max_steps else "reflect"

    def after_reflect(s: ReactState) -> str:
        return END if s.get("stopped") else "reason"

    g = StateGraph(ReactState)
    g.add_node("plan", plan)
    g.add_node("reason", reason)
    g.add_node("act", act)
    g.add_node("reflect", reflect)
    g.add_edge(START, "plan")
    g.add_conditional_edges("plan", after_plan, ["reason", END])
    g.add_conditional_edges("reason", after_reason, ["act", "reflect", END])
    g.add_conditional_edges("act", after_act, ["reason", "reflect"])
    g.add_conditional_edges("reflect", after_reflect, ["reason", END])
    return g.compile()


async def run(agent: str, system_prompt: str, user_prompt: str, query: str,
              ctx: tools_mod.ToolContext, max_steps: int = 4, max_tool_calls: int = 8) -> ReactState:
    """Run one specialist from its prompts; returns its final state. Never raises."""
    graph = build(agent, max_steps, max_tool_calls)
    init: ReactState = {"agent": agent, "query": query, "ctx": ctx, "steps": 0, "calls": 0,
                        "messages": [{"role": "system", "content": system_prompt},
                                     {"role": "user", "content": user_prompt}]}
    with tracing.run("agent.%s" % agent, "chain", inputs={"gathered": gathered_text(ctx)},
                     metadata={"max_steps": max_steps, "max_tool_calls": max_tool_calls,
                               "tools": sorted(tools_mod.for_agent(agent))}) as arun:
        try:
            final = await graph.ainvoke(init, config={"recursion_limit": 4 * max_steps + 10})
        except Exception as exc:                          # noqa: BLE001 -- never break a draft
            logger.warning("%s agent failed: %s", agent, exc)
            final = dict(init, stopped="error", notes=str(exc)[:300])
        if arun is not None:
            arun.end({"stopped": final.get("stopped"), "steps": final.get("steps"), "plan": final.get("plan"),
                      "path": final.get("path"), "tool_calls": [r.tool for r in final.get("records") or []],
                      "reflections": final.get("reflections")},
                     error=final.get("notes") if final.get("stopped") == "error" else None)
    return final
