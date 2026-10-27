"""The agent's tools: TicketMate's read-only capabilities, each with a name, a
description written for the model, a JSON schema for its arguments, and a
handler.

Nothing new is reachable here. Every tool wraps a function the fixed
pipeline already uses -- the cluster lookups (each validates its own
argument and runs one allowlisted, read-only command), rc_kg retrieval, and
the RC workflow playbooks -- so the agent can do exactly what the pipeline
could, in the order and with the arguments it chooses. Nothing that changes
anything (a reservation, a group, RC's scripts) is a tool, or can become one
by accident: those stay steps for the RC engineer.

A handler returns the text the model reads back (short: the model's context
is small) and records what it found in the ToolContext, which the draft is
then written from -- the same lookups list, sources and playbooks the
pipeline produces.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from . import kg_client, lookups, playbooks
from .lookups import Lookup
from .playbooks import Match

logger = logging.getLogger(__name__)

MAX_RESULT_CHARS = 1500        # what one tool result may put back into the agent's context
MAX_SOURCES = 8                # pipeline k=6, plus at most 2 the agent finds: each source is up to 2.5k chars


@dataclass
class ToolContext:
    """What the agent has gathered so far; starts from the pipeline's results."""
    ticket_text: str
    request_id: str = ""
    timeout: float = 5.0
    lookups: List[Lookup] = field(default_factory=list)
    kg: Optional["kg_client.KGContext"] = None
    playbooks: List[Match] = field(default_factory=list)
    calls: List[Dict[str, Any]] = field(default_factory=list)     # every tool call, for the trace
    cache: Dict[str, str] = field(default_factory=dict)            # (name, args) -> result text


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[[Dict[str, Any], ToolContext], Awaitable[str]]

    def schema(self) -> Dict[str, Any]:
        """The OpenAI/vLLM `tools` entry."""
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def _arg(name: str, description: str) -> Dict[str, Any]:
    return {"type": "object", "properties": {name: {"type": "string", "description": description}},
            "required": [name], "additionalProperties": False}


def _clip(text: str) -> str:
    return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + "\n... (truncated)"


def _seen(ctx: ToolContext, lk: Lookup) -> None:
    if not any(x.kind == lk.kind and x.target == lk.target for x in ctx.lookups):
        ctx.lookups.append(lk)


def _show(found: List[Lookup]) -> str:
    if not found:
        return "nothing found"
    return _clip(lookups.prompt_text(found))


def _lookup(kind: str, fn: Callable[..., Awaitable[Any]], arg: str):
    """A tool around one lookup function taking a single string argument."""
    async def handler(args: Dict[str, Any], ctx: ToolContext) -> str:
        lk = await fn(str(args.get(arg, "")), ctx.timeout)
        if lk is None:
            return "nothing to report"
        if lk.command == "(not run)":
            return lk.summary                     # "refused: ..." -- nothing ran
        _seen(ctx, lk)
        return _show([lk])
    return handler


async def _job(args: Dict[str, Any], ctx: ToolContext) -> str:
    """sacct, plus the batch script and the queue position, like the pipeline."""
    job_id = str(args.get("job_id", ""))
    lk = await lookups._job(job_id, ctx.timeout)
    if lk.command == "(not run)":
        return lk.summary
    found = [lk]
    if lk.ok:
        script = await lookups._job_script(job_id, ctx.timeout)
        if script.ok:
            found.append(script)
        queue = await lookups._job_queue(job_id, lk.summary.split(",")[0], ctx.timeout)
        if queue is not None:
            found.append(queue)
    for x in found:
        if x.command != "(not run)":
            _seen(ctx, x)
    return _show(found)


async def _group(args: Dict[str, Any], ctx: ToolContext) -> str:
    known = [x.target for x in ctx.lookups if x.kind == "user" and x.ok]
    lk = await lookups._group(str(args.get("group", "")), known, ctx.timeout)
    if lk.command == "(not run)":
        return lk.summary
    _seen(ctx, lk)
    return _show([lk])


async def _library(args: Dict[str, Any], ctx: ToolContext) -> str:
    found = await lookups._libraries([str(args.get("library", ""))], ctx.timeout)
    if found and found[0].command == "(not run)":
        return found[0].summary
    for x in found:
        if x.command != "(not run)":
            _seen(ctx, x)
    return _show(found)


async def _search(args: Dict[str, Any], ctx: ToolContext) -> str:
    query = str(args.get("query", "")).strip()[:300]
    if len(query) < 3:
        return "error: give a search query of a few words"
    res = await kg_client.retrieve(query, request_id=ctx.request_id)
    if res.status != "ok":
        return "search unavailable: %s" % (res.detail or res.status)
    before = {s.get("url") for s in (ctx.kg.sources if ctx.kg else [])}
    ctx.kg = kg_client.merge(ctx.kg, res, max_sources=MAX_SOURCES) if ctx.kg else res
    new = [s for s in ctx.kg.sources if s.get("url") not in before]
    if not new:
        return "no new pages (the ones found are already in your sources)"
    lines = ["%s: %s > %s -- %s" % (s["ref"], s.get("page_title", ""), s.get("heading", ""),
                                     (s.get("text") or "")[:160].replace("\n", " ")) for s in new]
    if res.low_confidence:
        lines.append("(weak match: these may not cover the question)")
    return _clip("\n".join(lines))


async def _playbook(args: Dict[str, Any], ctx: ToolContext) -> str:
    name = str(args.get("name", ""))
    book = next((b for b in playbooks.load() if b.name == name), None)
    if book is None:
        return "error: no playbook named %r; choose one of: %s" % (
            name, ", ".join(b.name for b in playbooks.load()))
    if not any(m.playbook.name == name for m in ctx.playbooks):
        ctx.playbooks.append(playbooks.Match(book, book.extract(ctx.ticket_text)))
    m = next(m for m in ctx.playbooks if m.playbook.name == name)
    missing = ("; missing from the ticket: " + "; ".join(m.missing)) if m.missing else ""
    return "loaded %s (its full procedure is given to the writer)%s" % (book.title, missing)


def registry() -> List[Tool]:
    """Every tool, in the order the model sees them."""
    books = playbooks.load()
    return [
        Tool("lookup_user", "Does an Explorer account exist, and which unix groups is it in (`id`).",
             _arg("username", "an Explorer username such as smith.j"), _lookup("user", lookups._user, "username")),
        Tool("lookup_job", "A job's state, exit code, run time, memory, node, its #SBATCH script and, if "
             "pending or running, its expected start or time left (sacct, squeue).",
             _arg("job_id", "a Slurm job ID, digits only"), _job),
        Tool("lookup_group", "A unix group's size and whether the ticket's users are in it.",
             _arg("group", "a unix group name such as gaussian"), _group),
        Tool("lookup_node", "A compute node's state, partitions, GPUs and Slurm's reason if it is down or "
             "drained (scontrol show node).", _arg("node", "a node name such as d4043 or c0602"),
             _lookup("node", lookups._node, "node")),
        Tool("lookup_partition", "How many nodes of a partition are up, and each down or drained node with "
             "its reason (sinfo).", _arg("partition", "a partition name such as gpu or short"),
             _lookup("partition", lookups._partition, "partition")),
        Tool("lookup_queue", "A user's jobs in the queue now, with expected start times and pending reasons.",
             _arg("username", "an Explorer username"), _lookup("queue", lookups._user_queue, "username")),
        Tool("lookup_fairshare", "A user's FairShare score and how their PI group's usage is spread "
             "(why jobs wait).", _arg("username", "an Explorer username"),
             _lookup("fairshare", lookups._fairshare, "username")),
        Tool("lookup_job_history", "A user's jobs in the last 7 days: counts by state, and the ones that "
             "did not complete.", _arg("username", "an Explorer username"),
             _lookup("history", lookups._history, "username")),
        Tool("lookup_project_owner", "Who owns a /projects directory (usually the PI).",
             _arg("project", "the directory name under /projects, e.g. smithlab"),
             _lookup("project", lookups._project_owner, "project")),
        Tool("lookup_library", "Whether a shared library (or another version of it) is installed.",
             _arg("library", "a library file name such as libcudnn.so.8"), _library),
        Tool("search_rc_docs", "Search the RC website and rc-docs for pages about something. Use your own "
             "short wording of the problem; results are added to the sources the reply may cite.",
             _arg("query", "a short search query, e.g. 'job killed out of memory'"), _search),
        Tool("load_playbook", "Load RC's procedure for a kind of request, when the ticket is one: " +
             "; ".join("%s = %s" % (b.name, b.title) for b in books if b.on == "researcher") + ".",
             {"type": "object", "properties": {"name": {"type": "string",
                                                         "enum": [b.name for b in books]}},
              "required": ["name"], "additionalProperties": False}, _playbook),
    ]


def by_name() -> Dict[str, Tool]:
    return {t.name: t for t in registry()}


async def call(name: str, args: Dict[str, Any], ctx: ToolContext,
               tools: Optional[Dict[str, Tool]] = None) -> Tuple[str, bool]:
    """Run one tool call; (result text, ok). Never raises: an unknown tool,
    bad arguments or a failure become a result the model can read."""
    tools = tools if tools is not None else by_name()
    key = name + json.dumps(args, sort_keys=True, default=str)
    if key in ctx.cache:
        return "(already done) " + ctx.cache[key], True
    tool = tools.get(name)
    if tool is None:
        return "error: no tool named %r" % name, False
    if "_unparsed" in args:
        return "error: the arguments were not valid JSON", False
    try:
        text = await tool.handler(args, ctx)
        ok = not text.startswith(("error:", "refused:"))
    except Exception as exc:                        # noqa: BLE001 -- a tool never breaks a draft
        logger.warning("tool %s failed: %s", name, exc)
        text, ok = "error: %s failed" % name, False
    ctx.cache[key] = text
    ctx.calls.append({"tool": name, "arguments": args, "ok": ok, "result_chars": len(text)})
    return text, ok
