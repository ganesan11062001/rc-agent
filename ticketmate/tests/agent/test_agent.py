"""The agent (backend/app/agent.py, backend/app/tools.py).

    backend/.venv/bin/python tests/agent/test_agent.py     exit 0 = pass

A scripted stand-in for the model (no GPU) and fake read-only binaries on
PATH, as in the lookups test. Proves: the tools are the read-only ones only;
the loop runs tool calls and stops; results are added to what the pipeline
gathered (sources renumbered, nothing lost); bad or hostile arguments run
nothing; budgets hold; and when the server has no tool calls, or anything
fails, the result is exactly the pipeline's.
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
else:
    sys.exit(1)
"""
for prog in ("scontrol", "id", "sacct", "getent", "sinfo", "squeue", "sshare", "stat"):
    p = fakebin / prog
    p.write_text(FAKE % {"log": str(calls_log)})
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
os.environ["PATH"] = str(fakebin) + ":/usr/bin:/bin"

from app import agent, kg_client, llm_client, lookups, playbooks, tools  # noqa: E402

fails = []


def check(cond, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


def ran():
    return [json.loads(l) for l in calls_log.read_text().splitlines()] if calls_log.exists() else []


def kgc(*titles, base=0, low=False):
    """A KGContext like rc_kg returns, with sources S<base+1>.. ."""
    srcs, blocks = [], []
    for i, t in enumerate(titles, base + 1):
        url = "https://rc-docs.example/%s" % t.lower().replace(" ", "-")
        srcs.append({"ref": "S%d" % (i - base), "url": url, "page_title": t, "heading": "Overview",
                     "text": "about " + t})
        blocks.append("[S%d] %s\nURL: %s\nabout %s" % (i - base, t, url, t))
    return kg_client.KGContext(status="ok", context="\n\n---\n\n".join(blocks), low_confidence=low,
                               sources=srcs, allowed_urls=[s["url"] for s in srcs], trace={})


class FakeModel:
    """Replays scripted turns; records the messages it was sent."""
    def __init__(self, turns, fail=None):
        self.turns, self.fail, self.seen = list(turns), fail, []

    async def __call__(self, messages, tools=None, max_tokens=600, name="llm.chat"):
        self.seen.append(json.loads(json.dumps(messages)))
        if self.fail:
            raise self.fail
        turn = self.turns.pop(0) if self.turns else {"content": "done: nothing more"}
        calls = [{"id": "c%d_%d" % (len(self.seen), i), "name": n, "arguments": a}
                 for i, (n, a) in enumerate(turn.get("calls", []))]
        return {"content": turn.get("content", ""), "tool_calls": calls, "finish_reason": "stop",
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}, "model": "fake"}


searches = []


async def fake_retrieve(query, request_id=""):
    searches.append(query)
    return kgc("GPUs on the HPC", "Partitions")          # one new page, one the seed already has


kg_client.retrieve = fake_retrieve
TICKET = ("Short description: d4043 GPU not working\n\nDescription:\nnvidia-smi hangs on d4043; "
          "my username is doe.j")
SEED_KG = kgc("Partitions", "Using CUDA")


def run(model, **kw):
    llm_client.chat = model
    return asyncio.run(agent.gather(TICKET, kw.pop("instructions", ""), [], SEED_KG, [], **kw))


print("tools")
reg = tools.registry()
names = [t.name for t in reg]
check(len(names) == len(set(names)) == 12, "12 distinct tools: %s" % names)
check(all(t.schema()["type"] == "function" and t.schema()["function"]["parameters"]["type"] == "object"
          for t in reg), "every tool has an OpenAI/vLLM function schema")
check(not any(w in n for n in names for w in ("add", "create", "delete", "update", "reserve", "run_", "exec",
                                               "push", "write", "remove")),
      "no tool changes anything (no add/create/delete/update/reserve/exec/push)")
pb_enum = [t for t in reg if t.name == "load_playbook"][0].parameters["properties"]["name"]["enum"]
check(set(pb_enum) == {b.name for b in playbooks.load()}, "load_playbook offers exactly the playbook files")

print("the loop")
m = FakeModel([{"calls": [("lookup_node", {"node": "d4043"}), ("search_rc_docs", {"query": "gpu not responding"})]},
               {"content": "done: node is drained for GPU ECC"}])
r = run(m)
check(r.stopped == "done" and r.steps == 2, "two turns, then done (%s, %d)" % (r.stopped, r.steps))
node = [lk for lk in r.lookups if lk.kind == "node"]
check(node and "DOWN+DRAIN" in node[0].summary, "the node lookup ran and is in what the writer gets")
check([s["ref"] for s in r.kg.sources] == ["S1", "S2", "S3"] and r.kg.sources[2]["page_title"] == "GPUs on the HPC",
      "search: the new page added as S3, the duplicate dropped: %s" % [(s["ref"], s["page_title"]) for s in r.kg.sources])
check("[S3] GPUs on the HPC" in r.kg.context and r.kg.context.count("[S2]") == 1,
      "the merged prompt context is renumbered too")
check(r.kg.allowed_urls[-1].endswith("gpus-on-the-hpc"), "and its URL may be cited")
second = m.seen[1]
check(second[-2]["role"] == "tool" and "DOWN+DRAIN" in second[-2]["content"]
      and second[-1]["role"] == "tool" and "S3:" in second[-1]["content"],
      "tool results go back to the model as tool messages")
check("Partitions" in m.seen[0][1]["content"] and "ALREADY GATHERED" in m.seen[0][1]["content"],
      "the model is told what the pipeline already gathered")
check(len(r.calls) == 2 and all(c["ok"] for c in r.calls), "both calls recorded for the trace")
check(r.usage.get("total_tokens") == 220, "token usage summed over the turns")

print("hostile and broken calls run nothing")
before = len(ran())
m = FakeModel([{"calls": [("lookup_node", {"node": "d4043; touch /tmp/pwned"}),
                          ("lookup_user", {"username": "$(id)"}),
                          ("lookup_job", {"job_id": "-u everyone"}),
                          ("no_such_tool", {"x": 1}),
                          ("lookup_node", {"_unparsed": "{node: d4043"}),
                          ("load_playbook", {"name": "rm -rf"})]}])
r = run(m)
check(len(ran()) == before, "no command ran for any of them")
res = [msg["content"] for msg in m.seen[1] if msg["role"] == "tool"]
check(res[0].startswith("refused:") and res[1].startswith("refused:") and res[2].startswith("refused:"),
      "invalid arguments are refused: %s" % [x[:40] for x in res[:3]])
check(res[3].startswith("error: no tool") and "not valid JSON" in res[4] and "no playbook named" in res[5],
      "unknown tool, bad JSON and a bad playbook name come back as errors")
check(not any(lk.kind in ("node", "user", "job") for lk in r.lookups), "nothing refused reaches the writer")
check(r.stopped == "done", "and the loop carries on to the end")

print("budgets and repeats")
loop = [{"calls": [("lookup_node", {"node": "d4043"})]}] * 10
r = run(FakeModel(loop), max_steps=3)
check(r.stopped == "step budget" and r.steps == 3, "stops at max_steps (%s)" % r.stopped)
check(len([lk for lk in r.lookups if lk.kind == "node"]) == 1, "a repeated call is not run or added twice")
many = [{"calls": [("search_rc_docs", {"query": "drained node %d" % i}),
                   ("search_rc_docs", {"query": "gpu ecc %d" % i})]} for i in range(5)]
r = run(FakeModel(many), max_steps=5, max_tool_calls=4)
check(r.stopped == "call budget" and len(r.calls) <= 4, "stops at max_tool_calls (%d calls)" % len(r.calls))

print("playbooks")
r = run(FakeModel([{"calls": [("load_playbook", {"name": "gaussian"})]}]))
check([m_.playbook.name for m_ in r.playbooks] == ["gaussian"], "load_playbook adds the playbook for the writer")
check(r.playbooks[0].values.get("username") == ["doe.j"], "with the values found in the ticket")

print("it can only add: fallbacks are the pipeline's result")
seed_lk = [lookups.Lookup("user", "doe.j", "id doe.j", True, "account exists")]
for fail, why in ((llm_client.ToolsUnsupported("no tools", "auto tool choice requires --enable-auto-tool-choice"),
                   "tools unsupported"), (RuntimeError("boom"), "error")):
    llm_client.chat = FakeModel([], fail=fail)
    r = asyncio.run(agent.gather(TICKET, "", seed_lk, SEED_KG, [], max_steps=3))
    check(r.stopped == why and r.lookups == seed_lk and r.kg is SEED_KG and r.playbooks == [],
          "%s: exactly the seed" % why)
r = run(FakeModel([{"content": "done: the gathered sources cover it"}]))
check(r.stopped == "done" and not r.calls and r.kg.sources == SEED_KG.sources, "no tool call: exactly the seed")
llm_client.chat = FakeModel([{"calls": [("search_rc_docs", {"query": "gpu"})]}])
r = asyncio.run(agent.gather(TICKET, "", [], kg_client.KGContext(status="unavailable"), []))
check(r.kg.status == "ok" and len(r.kg.sources) == 2, "seed had no sources: the agent's search becomes them")

print("follow-ups")
m = FakeModel([{"content": "done: a closing message needs nothing"}])
run(m, instructions="Write a short follow-up: if no questions we will close the ticket")
check("INSTRUCTIONS FROM THE RC ENGINEER" in m.seen[0][1]["content"], "the agent sees the engineer's instructions")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
