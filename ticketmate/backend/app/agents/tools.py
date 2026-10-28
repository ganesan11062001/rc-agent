"""The agents' tools: typed, scoped, read-only, traced.

Each tool is
- a pydantic model of its arguments -- the JSON schema the model is given
  is generated from it (types, formats, descriptions), and the model's
  arguments are validated against it before anything runs;
- a handler, wrapping a function TicketMate's pipeline already uses: the
  cluster lookups (each of which validates its argument again and runs one
  allowlisted, read-only program with a fixed argv, no shell), the knowledge
  graph (rc-kg `/retrieve`) and the skills;
- the agents allowed to use it.

Nothing that changes anything is a tool, or can become one: RC's scripts,
reservations, `project ... add members` stay steps for the RC engineer.

A call returns (text for the model, ToolCallRecord). The text is short; what
the call found goes into the run's Evidence, which the writer drafts from.
Every call is a `tool.<name>` run in the trace with its arguments, result,
latency and whether it ran.
"""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Literal, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from .. import kg_client, lookups, tracing
from . import skills
from .state import Evidence, ToolCallRecord

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 1500       # what one result may put back into the agent's context
MAX_SOURCES = 8               # rules give 6; agents add at most 2 (each source is up to 2.5k chars)
INCIDENT, REQUEST = "incident", "request"


# --- Argument models: the function-calling schemas ---------------------------

def _full(pattern) -> str:
    """Anchored: pydantic's `pattern` is a search, the lookups' check a full match."""
    return "^(?:%s)$" % pattern.pattern


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserArgs(_Args):
    username: str = Field(pattern=_full(lookups.USERNAME), description="An Explorer username, e.g. smith.j")


class JobArgs(_Args):
    job_id: str = Field(pattern=_full(lookups.JOB_ID), description="A Slurm job ID, digits only")


class GroupArgs(_Args):
    group: str = Field(pattern=_full(lookups.GROUP), description="A unix group, e.g. gaussian")


class NodeArgs(_Args):
    node: str = Field(pattern=_full(lookups.NODE), description="A compute node, e.g. d4043 or c0602")


class PartitionArgs(_Args):
    partition: str = Field(pattern=_full(lookups.PARTITION), description="A Slurm partition, e.g. gpu, short")


class ProjectArgs(_Args):
    project: str = Field(pattern=_full(lookups.PROJECT), description="The directory under /projects, e.g. smithlab")


class LibraryArgs(_Args):
    library: str = Field(pattern=_full(lookups.LIBRARY), description="A shared library, e.g. libcudnn.so.8")


class SearchArgs(_Args):
    query: str = Field(min_length=3, max_length=200,
                       description="A short search in your own words, e.g. 'job killed out of memory'")


# --- The run's context --------------------------------------------------------

@dataclass
class ToolContext:
    """One draft's investigation: the evidence so far and the calls made."""
    ticket: str
    evidence: Evidence
    request_id: str = ""
    timeout: float = 5.0
    cache: Dict[str, str] = field(default_factory=dict)


@dataclass
class Tool:
    name: str
    description: str
    args: Type[_Args]
    handler: Callable[[_Args, ToolContext], Awaitable[str]]
    agents: Tuple[str, ...] = (INCIDENT, REQUEST)

    def schema(self) -> Dict[str, Any]:
        """The OpenAI/vLLM `tools` entry, generated from the argument model."""
        params = self.args.model_json_schema()
        params.pop("title", None)
        for p in params.get("properties", {}).values():
            p.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": params}}


# --- Handlers -------------------------------------------------------------------

def _clip(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n... (truncated)"


def _keep(ctx: ToolContext, found: List[lookups.Lookup]) -> str:
    ran = [lk for lk in found if lk is not None and lk.command != "(not run)"]
    for lk in ran:
        if not any(x.kind == lk.kind and x.target == lk.target for x in ctx.evidence.lookups):
            ctx.evidence.lookups.append(lk)
    refused = [lk for lk in found if lk is not None and lk.command == "(not run)"]
    if refused:
        return refused[0].summary
    return _clip(lookups.prompt_text(ran)) if ran else "nothing found"


def _one(fn: Callable, attr: str):
    async def handler(a: _Args, ctx: ToolContext) -> str:
        return _keep(ctx, [await fn(getattr(a, attr), ctx.timeout)])
    return handler


async def _job(a: JobArgs, ctx: ToolContext) -> str:
    lk = await lookups._job(a.job_id, ctx.timeout)
    found = [lk]
    if lk.ok:
        found.append(await lookups._job_script(a.job_id, ctx.timeout))
        found.append(await lookups._job_queue(a.job_id, lk.summary.split(",")[0], ctx.timeout))
    return _keep(ctx, [x for x in found if x is not None and (x.ok or x.kind == "job")])


async def _group(a: GroupArgs, ctx: ToolContext) -> str:
    known = [x.target for x in ctx.evidence.lookups if x.kind == "user" and x.ok]
    return _keep(ctx, [await lookups._group(a.group, known, ctx.timeout)])


async def _library(a: LibraryArgs, ctx: ToolContext) -> str:
    return _keep(ctx, await lookups._libraries([a.library], ctx.timeout))


async def _search(a: SearchArgs, ctx: ToolContext) -> str:
    """The knowledge graph: rc-kg's hybrid vector + fulltext + topic retrieval."""
    res = await kg_client.retrieve(a.query, request_id=ctx.request_id)
    if res.status != "ok":
        return "knowledge graph unavailable: %s" % (res.detail or res.status)
    have = ctx.evidence.kg
    before = {s.get("url") for s in (have.sources if have and have.status == "ok" else [])}
    ctx.evidence.kg = kg_client.merge(have, res, max_sources=MAX_SOURCES) if have and have.status == "ok" else res
    new = [s for s in ctx.evidence.kg.sources if s.get("url") not in before]
    if not new:
        return "no new pages: what it found is already in your sources"
    lines = ["%s: %s > %s -- %s" % (s["ref"], s.get("page_title", ""), s.get("heading", ""),
                                     (s.get("text") or "")[:160].replace("\n", " ")) for s in new]
    if res.low_confidence:
        lines.append("(weak match: these pages may not cover the question)")
    return _clip("\n".join(lines))


async def _skill(a: _Args, ctx: ToolContext) -> str:
    names = [s.name for s in skills.catalog()]
    if a.name not in names:
        return "error: no skill named %r; the skills are: %s" % (a.name, ", ".join(names))
    m = next((m for m in ctx.evidence.skills if m.playbook.name == a.name), None)
    if m is None:
        m = skills.load(a.name, ctx.ticket)
        ctx.evidence.skills.append(m)
    missing = ("; missing from the ticket: " + "; ".join(m.missing)) if m.missing else ""
    return "loaded skill %s (the writer gets its full procedure)%s" % (m.playbook.title, missing)


# --- The registry ---------------------------------------------------------------

def registry() -> List[Tool]:
    """Every tool. `for_agent` gives one agent's subset."""
    procs = skills.procedures()
    # The skill names as a Literal: the schema lists them, and validation
    # refuses any other name before the handler runs.
    skill_args = create_model("SkillArgs", __base__=_Args,
                              name=(Literal[tuple(s.name for s in procs)], Field(description="The skill to load")))
    return [
        Tool("lookup_user", "Does an Explorer account exist, and which unix groups is it in (`id`).",
             UserArgs, _one(lookups._user, "username")),
        Tool("lookup_job", "A job's state, exit code, run time, memory, node, its #SBATCH script and, if pending "
             "or running, the expected start or time left (sacct, squeue).", JobArgs, _job, (INCIDENT,)),
        Tool("lookup_group", "A unix group's size, and whether the ticket's users are in it.", GroupArgs, _group),
        Tool("lookup_node", "A compute node's state, partitions, GPUs and Slurm's reason if it is down or "
             "drained (scontrol show node).", NodeArgs, _one(lookups._node, "node"), (INCIDENT,)),
        Tool("lookup_partition", "How many of a partition's nodes are up, and each down or drained node with "
             "its reason (sinfo).", PartitionArgs, _one(lookups._partition, "partition")),
        Tool("lookup_queue", "A user's jobs in the queue now, with expected start times and pending reasons.",
             UserArgs, _one(lookups._user_queue, "username"), (INCIDENT,)),
        Tool("lookup_fairshare", "A user's FairShare score and how their PI group's usage is spread -- why "
             "jobs wait.", UserArgs, _one(lookups._fairshare, "username"), (INCIDENT,)),
        Tool("lookup_job_history", "A user's jobs in the last 7 days: counts by state, and the ones that did "
             "not complete.", UserArgs, _one(lookups._history, "username"), (INCIDENT,)),
        Tool("lookup_project_owner", "Who owns a /projects directory -- usually the PI.",
             ProjectArgs, _one(lookups._project_owner, "project")),
        Tool("lookup_library", "Whether a shared library, or another version of it, is installed.",
             LibraryArgs, _library, (INCIDENT,)),
        Tool("search_knowledge_graph", "Search RC's knowledge graph -- the RC website and rc-docs -- for pages "
             "about something, in your own short words. New pages are added to the sources the reply may cite.",
             SearchArgs, _search),
        Tool("load_skill", "Load RC's procedure for a kind of request, when the ticket is one: " +
             "; ".join("%s = %s" % (s.name, s.title) for s in procs) + ".", skill_args, _skill),
    ]


def for_agent(agent: str) -> Dict[str, Tool]:
    return {t.name: t for t in registry() if agent in t.agents}


async def call(agent: str, step: int, name: str, raw_args: Dict[str, Any], ctx: ToolContext,
               tools: Dict[str, Tool]) -> Tuple[str, ToolCallRecord]:
    """Validate and run one tool call; never raises. Traced as `tool.<name>`."""
    t0 = time.perf_counter()
    key = name + json.dumps(raw_args, sort_keys=True, default=str)

    def record(text: str, ok: bool, cached: bool = False) -> ToolCallRecord:
        return ToolCallRecord(agent=agent, step=step, tool=name, arguments=raw_args, ok=ok,
                              result=text[:MAX_RESULT_CHARS], cached=cached,
                              latency_ms=int((time.perf_counter() - t0) * 1000))

    with tracing.run("tool." + name, "tool", inputs=raw_args,
                     metadata={"agent": agent, "step": step, "read_only": True}) as run:
        if key in ctx.cache:
            text = "(already done) " + ctx.cache[key]
            rec = record(text, True, cached=True)
        elif name not in tools:
            text = "error: no tool %r for the %s agent" % (name, agent)
            rec = record(text, False)
        elif "_unparsed" in raw_args:
            text = "error: the arguments were not valid JSON"
            rec = record(text, False)
        else:
            tool = tools[name]
            try:
                args = tool.args.model_validate(raw_args)
            except ValidationError as exc:
                text = "refused: invalid arguments -- " + "; ".join(
                    "%s: %s" % (".".join(map(str, e["loc"])), e["msg"]) for e in exc.errors())[:300]
                rec = record(text, False)
            else:
                try:
                    text = await tool.handler(args, ctx)
                    ok = not text.startswith(("error:", "refused:"))
                except Exception as exc:              # noqa: BLE001 -- a tool never breaks a draft
                    logger.warning("tool %s failed: %s", name, exc)
                    text, ok = "error: %s failed" % name, False
                ctx.cache[key] = text
                rec = record(text, ok)
        if run is not None:
            run.end({"result": rec.result, "ok": rec.ok, "cached": rec.cached},
                    error=None if rec.ok else rec.result[:200])
    return text, rec
