"""The agent graph (backend/app/agents): tools, the ReAct subgraph, the
orchestrator, the critic, the reviewer and the whole graph.

    backend/.venv/bin/python tests/agent/test_agent.py     exit 0 = pass

No GPU: a scripted model answers each agent by name (agent.orchestrator,
agent.incident.plan / .step, agent.request.*, agent.reviewer), and fake
read-only binaries on PATH record every command, as in the lookups test.
"""
import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "backend"))

# RC's playbooks are internal and live outside the repository (cluster/env.sh sets PLAYBOOK_DIR).
import os as _os  # noqa: E402
_PRIVATE = "/projects/rc/projects/rc-copilot/shared/playbooks"
if not _os.environ.get("PLAYBOOK_DIR") and _os.path.isdir(_PRIVATE):
    _os.environ["PLAYBOOK_DIR"] = _PRIVATE
if not _os.environ.get("PLAYBOOK_DIR"):
    print("SKIP: RC's playbooks are not available here (set PLAYBOOK_DIR)")
    raise SystemExit(0)

work = Path(tempfile.mkdtemp())
fakebin = work / "bin"
fakebin.mkdir()
calls_log = work / "calls.jsonl"
FAKE = """#!/usr/bin/env python3
import json, sys
open(%(log)r, "a").write(json.dumps(sys.argv) + "\\n")
name, args = sys.argv[0].rsplit("/", 1)[-1], sys.argv[1:]
if name == "scontrol" and args == ["show", "node", "d4043"]:
    print("NodeName=d4043 State=DOWN+DRAIN Partitions=gpu Reason=GPU_ECC [root@2026-10-20T11:12:13]")
elif name == "id" and args == ["doe.j"]:
    print("uid=1(doe.j) gid=100(users) groups=100(users)")
elif name == "sacct" and args[:2] == ["-j", "4812345"] and "--batch-script" not in args:
    print("JobID|JobName|User|Account|Partition|State|ExitCode|Elapsed|Timelimit|ReqMem|MaxRSS|AllocCPUS|NodeList|Start|End|WorkDir")
    print("4812345|train|doe.j|lab|short|OUT_OF_MEMORY|0:125|00:12:01|04:00:00|4G||4|c0123|x|y|/home/doe.j")
else:
    sys.exit(1)
"""
for prog in ("scontrol", "id", "sacct", "getent", "sinfo", "squeue", "sshare", "stat"):
    p = fakebin / prog
    p.write_text(FAKE % {"log": str(calls_log)})
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
os.environ["PATH"] = str(fakebin) + ":/usr/bin:/bin"

from app import kg_client, llm_client, lookups  # noqa: E402
from app.agents import critic, graph, react, reviewer, skills, tools  # noqa: E402
from app.agents.state import Evidence, Review  # noqa: E402

fails = []


def check(cond, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


def ran():
    return [json.loads(l) for l in calls_log.read_text().splitlines()] if calls_log.exists() else []


def kgc(*titles, low=False):
    srcs, blocks = [], []
    for i, t in enumerate(titles, 1):
        url = "https://rc-docs.example/%s" % t.lower().replace(" ", "-")
        srcs.append({"ref": "S%d" % i, "url": url, "page_title": t, "heading": "Overview", "text": "about " + t})
        blocks.append("[S%d] %s\nURL: %s\nabout %s" % (i, t, url, t))
    return kg_client.KGContext(status="ok", context="\n\n---\n\n".join(blocks), low_confidence=low,
                               sources=srcs, allowed_urls=[s["url"] for s in srcs], trace={})


async def fake_retrieve(query, request_id=""):
    return kgc("Out of Memory", "Partitions")


kg_client.retrieve = fake_retrieve


class Model:
    """Scripted chat(): `script[name]` is a list of turns for that agent call name."""
    def __init__(self, script, fail=None):
        self.script = {k: list(v) for k, v in script.items()}
        self.fail, self.seen = fail, []

    async def __call__(self, messages, tools=None, max_tokens=600, name="llm.chat", json_schema=None):
        self.seen.append((name, json.loads(json.dumps(messages)), [t["function"]["name"] for t in tools or []]))
        if self.fail and (self.fail[0] is None or self.fail[0] == name):
            raise self.fail[1]
        turns = self.script.get(name) or []
        turn = turns.pop(0) if turns else {"content": "done: nothing more"}
        calls = [{"id": "c%d_%d" % (len(self.seen), i), "name": n, "arguments": a}
                 for i, (n, a) in enumerate(turn.get("calls", []))]
        return {"content": turn.get("content", ""), "tool_calls": calls, "finish_reason": "stop",
                "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55}, "model": "fake"}


def ctx_for(ticket, seed=None):
    return tools.ToolContext(ticket=ticket, evidence=seed or Evidence(kg=kgc("Partitions")))


# ---------------------------------------------------------------------------
print("tools: typed, scoped, read-only")
reg = tools.registry()
check(len(reg) == 12 and len({t.name for t in reg}) == 12, "12 distinct tools")
check(not any(w in t.name for t in reg for w in ("add", "create", "delete", "update", "reserve", "exec", "push",
                                                 "write", "remove", "grant")), "no tool changes anything")
inc, req = tools.for_agent("incident"), tools.for_agent("request")
check("lookup_job" in inc and "lookup_job" not in req and "load_skill" in req and "search_knowledge_graph" in req,
      "each agent gets its own tools (request: %s)" % sorted(req))
sch = inc["lookup_node"].schema()["function"]["parameters"]
check(sch["properties"]["node"]["pattern"].startswith("^") and sch["additionalProperties"] is False,
      "schemas are generated from the argument models, anchored, closed")
enum = req["load_skill"].schema()["function"]["parameters"]["properties"]["name"].get("enum")
check(set(enum) == {s.name for s in skills.procedures()}, "load_skill offers exactly the procedure skills")

before = len(ran())
c = ctx_for("x")
for name, args in (("lookup_node", {"node": "d4043; touch /tmp/pwned"}), ("lookup_user", {"username": "$(id)"}),
                   ("lookup_job", {"job_id": "-u everyone"}), ("lookup_node", {"node": "d4043", "extra": 1}),
                   ("load_skill", {"name": "rm -rf"}), ("lookup_job", {"job_id": "4812345"})):
    text, rec = asyncio.run(tools.call("request" if name != "lookup_job" or "-u" in str(args) else "request",
                                       1, name, args, c, req if name == "lookup_job" else inc | req))
    check(not rec.ok and (text.startswith("refused:") or text.startswith("error:")),
          "refused, nothing run: %s %s -> %s" % (name, args, text[:60]))
check(len(ran()) == before, "no command ran for any hostile or out-of-scope call")
text, rec = asyncio.run(tools.call("incident", 1, "lookup_node", {"node": "d4043"}, c, inc))
text2, rec2 = asyncio.run(tools.call("incident", 2, "lookup_node", {"node": "d4043"}, c, inc))
check(rec.ok and "DOWN+DRAIN" in text and rec2.cached and len([lk for lk in c.evidence.lookups if lk.kind == "node"]) == 1,
      "a real call runs once, is kept as evidence, and a repeat is served from cache")
text, rec = asyncio.run(tools.call("request", 1, "search_knowledge_graph", {"query": "job killed memory"}, c, req))
check(rec.ok and [s["page_title"] for s in c.evidence.kg.sources] == ["Partitions", "Out of Memory"]
      and c.evidence.kg.sources[1]["ref"] == "S2", "knowledge-graph search adds new pages, renumbered")

# ---------------------------------------------------------------------------
print("ReAct subgraph: plan, act/observe, reflect")
TICKET = "Short description: job 4812345 died\n\nDescription:\nMy job 4812345 was killed, node d4043 was slow."
QUERY = "job 4812345 died\n\nMy job 4812345 was killed, node d4043 was slow."
m = Model({"agent.incident.plan": [{"content": "1. job state (lookup_job)\n2. node (lookup_node)"}],
           "agent.incident.step": [{"calls": [("lookup_node", {"node": "d4043"})]},
                                   {"content": "done: node drained"},          # forgets the job -> reflection
                                   {"calls": [("lookup_job", {"job_id": "4812345"})]},
                                   {"content": "done: OOM, node drained"}]})
llm_client.chat = m
c = ctx_for(TICKET)
final = asyncio.run(react.run("incident", "SYSTEM", "USER", QUERY, c, max_steps=6, max_tool_calls=8))
check(final["plan"].startswith("1. job state"), "the plan turn ran first, without tools")
check(m.seen[0][2] == [] and m.seen[1][2] and "lookup_job" in m.seen[1][2], "plan has no tools; reasoning has them")
check(final["path"][:3] == ["plan", "reason", "act"] and "reflect" in final["path"], "path: %s" % final["path"])
check(any("job 4812345" in r for r in final["reflections"]), "reflection caught the named job not looked up")
check([r.tool for r in final["records"]] == ["lookup_node", "lookup_job"] and final["stopped"] == "done",
      "it then looked it up and stopped (%s)" % [r.tool for r in final["records"]])
check(any(lk.kind == "job" and "OUT_OF_MEMORY" in lk.summary for lk in c.evidence.lookups), "the job is evidence")

llm_client.chat = Model({"agent.incident.plan": [{"content": "go"}],
                         "agent.incident.step": [{"calls": [("search_knowledge_graph", {"query": "q%d" % i})]}
                                                 for i in range(20)]})
final = asyncio.run(react.run("incident", "S", "U", "q", ctx_for("t"), max_steps=3, max_tool_calls=8))
check(final["steps"] <= 4 and len(final["records"]) <= 4, "budgets hold (steps %s)" % final["steps"])
llm_client.chat = Model({}, fail=("agent.incident.step", llm_client.ToolsUnsupported("no tools", "auto tool choice")))
final = asyncio.run(react.run("incident", "S", "U", "q", ctx_for("t"), max_steps=3))
check(final["stopped"] == "tools unsupported" and not final.get("records"), "no tool calls on the server: stops cleanly")

# ---------------------------------------------------------------------------
print("orchestrator")
def route(ticket, query, instructions="", ev=None, script=None, number=""):
    llm_client.chat = Model(script or {})
    return asyncio.run(graph._route({"ticket": ticket, "query": query, "instructions": instructions,
                                     "ticket_number": number, "evidence": ev or Evidence()}))
r = route("t", "q", "Write a short follow-up: if no questions we will close the ticket")
check(r["route"] == "followup" and r["route_reason"].startswith("rules"), "follow-up instruction -> followup (rules)")
aicr = skills.load("aicr-access", "t")
r = route("t", "q", ev=Evidence(skills=[aicr]))
check(r["route"] == "request" and "aicr-access" in r["route_reason"], "a triggered request skill -> request")
r = route("Ticket: TASK09000001\n...", "q")
check(r["route"] == "request" and "TASK" in r["route_reason"], "a TASK ticket -> request")
r = route("t", "my job hangs", script={"agent.orchestrator": [{"content": '{"route": "incident", "reason": "a hang"}'}]})
check(r["route"] == "incident" and r["route_reason"].startswith("model"), "otherwise the model decides")
llm_client.chat = Model({}, fail=(None, RuntimeError("down")))
r = asyncio.run(graph._route({"ticket": "t", "query": "q", "instructions": "", "evidence": Evidence()}))
check(r["route"] == "incident" and "default" in r["route_reason"], "model unavailable -> incident (default)")

# ---------------------------------------------------------------------------
print("critic (rules)")
STAFF = "x\n\n[2026-10-20 10:00] Doe, Jo, RC staff (comment, visible to the researcher):\nWe are looking into it.\n"
good = {"draft_response": "Hi Ada,\n\nRC is looking into node d4043.\n\nBest regards,\nResearch Computing"}
check(critic.review(good, STAFF) == [], "a good reply passes")
p = critic.review({"draft_response": "Hi,\n\nWe have granted you access.\n\nBest regards,\nResearch Computing"}, STAFF)
check(p and "granted" in p[0], "claims an action nobody took -> flagged")
ok = critic.review({"draft_response": "Hi,\n\nWe have added you to gaussian.\n\nBest regards,\nResearch Computing"},
                   STAFF.replace("looking into it", "added you to gaussian"))
check(ok == [], "the same claim with an RC staff entry saying so -> fine")
p = critic.review({"draft_response": "Hi,\n\nPlease contact the systems team.\n\nBest regards,\nResearch Computing"}, STAFF)
check(any("systems team" in x for x in p), "sends them to the systems team -> flagged")
check(not critic.review({"draft_response": "Hi,\n\nPlease reach out to rchelp if anything comes up.\n\nBest regards,"
                                           "\nResearch Computing"}, STAFF, closing=True), "rchelp in a closing -> fine")
p = critic.review({"draft_response": "Hi,\n\nWe will add them.\n\nBest regards,\nResearch Computing"}, STAFF,
                  missing=["the email address of each person to add"])
check(any("must ask" in x for x in p), "missing information not asked for -> flagged")
p = critic.review({"draft_response": "Hi,\n\nOK.\n\nThanks,\n[Your Name]"}, STAFF)
check(any("Best regards" in x for x in p), "placeholder / wrong sign-off -> flagged")

# ---------------------------------------------------------------------------
print("reviewer agent")
llm_client.chat = Model({"agent.reviewer": [{"content": json.dumps({"unsupported_claims": ["partition limit is 8 h"],
                                                                     "problems": [], "supported_share": 0.6})}]})
v = asyncio.run(reviewer.review({"draft_response": "The partition limit is 8 h."}, "t", Evidence()))
check(not v.ok and v.unsupported_claims == ["partition limit is 8 h"] and v.score == 0.6,
      "unsupported claims become problems, with the supported share")
sent = llm_client.chat.seen[0][1][1]["content"]
check("EVIDENCE: CLUSTER LOOKUPS" in sent and "DRAFT TO REVIEW" in sent, "it is shown the evidence and the draft")
llm_client.chat = Model({"agent.reviewer": [{"content": "I think it is fine overall."}]})
v = asyncio.run(reviewer.review({"draft_response": "x"}, "t", Evidence()))
check(v.ok and v.detail.startswith("not reviewed"), "an unparseable verdict never blocks the draft")

# ---------------------------------------------------------------------------
print("the whole graph")
writes = []


async def writer(evidence, notes, route):
    writes.append((notes, route, [lk.command for lk in evidence.lookups]))
    reply = "Hi,\n\nYour job ran out of memory.\n\nBest regards,\nResearch Computing"
    if not notes:
        reply = "Hi,\n\nYour job ran out of memory on the 8 h gpu partition.\n\nBest regards,\nResearch Computing"
    return {"problem_summary": "OOM", "suggested_steps": ["raise --mem"], "confidence": 0.8, "caveats": [],
            "draft_response": reply}, "fake"


script = {"agent.orchestrator": [{"content": '{"route": "incident", "reason": "job killed"}'}],
          "agent.incident.plan": [{"content": "1. job (lookup_job)"}],
          "agent.incident.step": [{"calls": [("lookup_job", {"job_id": "4812345"}),
                                             ("lookup_node", {"node": "d4043"})]},
                                  {"content": "done: OOM"}],
          "agent.reviewer": [{"content": json.dumps({"unsupported_claims": ["the 8 h gpu partition"], "problems": [],
                                                     "supported_share": 0.5})},
                             {"content": json.dumps({"unsupported_claims": [], "problems": [], "supported_share": 1})}]}
llm_client.chat = Model(script)
final = asyncio.run(graph.run(writer, request_id="r1", ticket=TICKET, query=QUERY, instructions="",
                              ticket_number="INC1", evidence=Evidence(kg=kgc("Partitions"))))
check(final["route"] == "incident", "routed to the incident agent")
check(final["path"][:2] == ["orchestrator", "incident_agent"] and "revise" in final["path"] and final["path"][-1] == "review",
      "path: %s" % final["path"])
check(len(writes) == 2 and writes[1][0] and "8 h gpu partition" in writes[1][0],
      "the reviewer's unsupported claim went back to the writer as a revision note")
check("8 h" not in final["draft"]["draft_response"] and final["revisions"] == 1, "the revised draft is the result")
check(any("sacct -j 4812345" in c for c in writes[0][2]), "the writer drafted from the agent's evidence")
prov = graph.provenance(final)
check(prov["route_reason"].startswith("model") and prov["plan"] and len(prov["tool_calls"]) == 2
      and len(prov["reviews"]) == 6 and prov["usage"]["total_tokens"] > 0 and set(prov["manifest"]) ==
      {"prompts", "skills", "tools"}, "provenance: route, plan, calls, 2 rounds x (critic, guardrails, reviewer), usage, versions")

writes.clear()
llm_client.chat = Model({"agent.reviewer": [{"content": json.dumps({"unsupported_claims": ["x"], "problems": [],
                                                                     "supported_share": 0})}] * 5})
final = asyncio.run(graph.run(writer, request_id="r2", ticket="Ticket: INC2\nt", query="q",
                              instructions="Just following up on the previous message", ticket_number="INC2",
                              evidence=Evidence(), max_revisions=1))
check(final["route"] == "followup" and "incident_agent" not in final["path"] and "request_agent" not in final["path"],
      "a follow-up skips the specialists")
check(len(writes) <= 2, "revisions are bounded (%d writes)" % len(writes))

writes.clear()
llm_client.chat = Model({}, fail=(None, llm_client.ToolsUnsupported("no tools", "x")))
final = asyncio.run(graph.run(writer, request_id="r3", ticket=TICKET, query=QUERY, instructions="",
                              ticket_number="INC3", evidence=Evidence(kg=kgc("Partitions"))))
check(final.get("draft") and len(writes) >= 1, "a server with no tool calls: the draft is still written")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
