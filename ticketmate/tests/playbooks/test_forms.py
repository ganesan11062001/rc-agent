"""Request-type extraction from a catalog item's request form (playbooks.py).

    backend/.venv/bin/python tests/playbooks/test_forms.py    exit 0 = pass

Self-contained: an inline playbook, no PLAYBOOK_DIR needed. A form answer
fills its field before any pattern over the thread does, so a multi-GPU form
and an access form each yield the values their own questions ask for.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app import playbooks, ticket_context  # noqa: E402

fails = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


BOOK = playbooks.parse(r"""---
name: scratch-gpu
title: Example GPU partition request
match: (?i)\bexample gpu\b
field.username: (?<![\w@./-])([a-z][a-z0-9-]*\.[a-z][a-z0-9-]*)(?![\w@-]|\.[a-z0-9])
form.username: (?i)\buser ?name\b
form.reason: (?i)\b(describe|reason)\b
form.runtime: (?i)\brun ?time\b
require.username: their Explorer username
require.reason: why they need it
rubric: It does not grant access in the reply.
---
The runbook.
""", "scratch-gpu.md")

FORM = """Ticket: TASK0100001

Short description: Example GPU partition access

Request form:
- Username: doe.l (Lee Doe)
- Describe your work: DDP training,
scales to 4 GPUs
- Expected run time: 5 days

Work notes / activity:
Rivera, Sam
Work notes•10/21/2026 09:00:00 AM
cc lee.k and smith.j"""

print("form answers")
p = ticket_context.prepare(FORM)
qa = playbooks.form_answers(p.text)
check([q for q, _ in qa] == ["Username", "Describe your work", "Expected run time"], "questions in order: %s" % qa)
check(qa[1][1] == "DDP training,\nscales to 4 GPUs", "a multi-line answer is kept whole")
check(playbooks.form_answers("no form here") == [], "no form: nothing")

print("values come from the form first")
m = playbooks.find(p.query, p.text, [BOOK])[0]
check(m.values.get("username") == ["doe.l"], "username from its answer, picked out by the pattern: %s" % m.values)
check("lee.k" not in str(m.values) and "smith.j" not in str(m.values), "names elsewhere in the thread are not taken")
check(m.sources == {"username": "form: Username", "reason": "form: Describe your work",
                    "runtime": "form: Expected run time"}, "where each value came from: %s" % m.sources)
check(not m.missing, "nothing missing")
check("(the researcher's answer on the request form)" in playbooks.prompt_text([m]), "the prompt says the value is the form's")
check(m.to_dict()["value_sources"] == m.sources and m.to_dict()["rubric"] == ["It does not grant access in the reply."],
      "the trace keeps sources and rubric")

print("without a form: the patterns, and what is missing")
t = "Ticket: INC0000042\n\nShort description: example gpu access please\n\nDescription:\nI am doe.l"
m2 = playbooks.find("example gpu access please I am doe.l", t, [BOOK])[0]
check(m2.values.get("username") == ["doe.l"] and m2.sources.get("username") == "text", "username from the text")
check(m2.missing == ["why they need it"], "a form-only field is asked for: %s" % m2.missing)
items = playbooks.rubric_items([m2])
check(items[0].startswith("It follows RC's procedure for Example GPU partition request")
      and "It does not grant access in the reply." in items
      and any("why they need it" in i for i in items), "rubric: procedure, playbook lines, the missing item")

print("header rules")
try:
    playbooks.parse("---\nname: x\ntitle: X\nmatch: x\nrequire.nope: y\n---\nb", "x.md")
    bad = False
except ValueError as e:
    bad = "form.nope" in str(e)
check(bad, "require.<field> must name a field.* or form.* line")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
