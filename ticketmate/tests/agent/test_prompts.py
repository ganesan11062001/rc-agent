"""Prompt lint: every agent prompt (backend/app/agents/prompts) and the writer's.

    backend/.venv/bin/python tests/agent/test_prompts.py     exit 0 = pass

Templates have exactly the placeholders their callers fill; system prompts
have none to fill and stay within a length budget (a 7B model with a 16-32k
window reads every token of them on every turn); the rules that keep the
agents safe are actually stated; a variant directory overrides same-named
prompts and nothing else.
"""
import os
import sys
import tempfile
from pathlib import Path
from string import Formatter

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app import prompts as writer  # noqa: E402
from app.agents import prompt_registry as reg  # noqa: E402

fails = []


def check(cond, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


def fields(text):
    return {f for _, f, _, _ in Formatter().parse(text) if f}


ALL = reg.load_all()
TEMPLATES = {"react_user": {"ticket", "instructions", "gathered"}, "react_act": {"plan"}, "react_reflect": {"gaps"},
             "reviewer_user": {"ticket", "lookups", "sources", "skills", "summary", "steps", "reply"}, "react_plan": set()}
SYSTEMS = {"orchestrator", "incident", "request", "reviewer"}
check(set(ALL) == set(TEMPLATES) | SYSTEMS, "every prompt file is known: %s" % sorted(ALL))
for name, want in TEMPLATES.items():
    check(fields(ALL[name].text) == want, "%s fills exactly %s" % (name, sorted(want)))
    ALL[name].render(**{k: "x" for k in want})
for name in SYSTEMS:
    text = ALL[name].text
    check(len(text) < 3200, "%s is within budget (%d chars)" % (name, len(text)))
    check(len(ALL[name].version) == 8, "%s has a version (%s)" % (name, ALL[name].version))
for name in ("incident", "request"):
    t = ALL[name].text.lower()
    check("cannot change anything" in t or "cannot grant or change anything" in t, "%s says it is read-only" % name)
    check("never guess" in t, "%s forbids guessing names and IDs" % name)
check('"route"' in ALL["orchestrator"].text and "Examples" in ALL["orchestrator"].text,
      "the orchestrator states its JSON and has examples")
check("unsupported_claims" in ALL["reviewer"].text and "do not rewrite" in ALL["reviewer"].text.lower(),
      "the reviewer states its JSON and that it does not rewrite")
w = writer.SYSTEM_PROMPT
check("Never say something has been done" in w and "Never send the researcher to Research Computing" in w,
      "the writer states the no-false-claims and no-redirect rules")
msg = writer.build_messages("T", kg_context="[S1] x", lookups_context="$ id a", playbook_context="### P",
                            playbook_missing=("an email",), revision_notes="- fix", extra_instructions="do x")
check(all("{" + k + "}" not in m["content"] for m in msg for k in ("ticket_text", "context", "notes", "playbooks")),
      "the writer's prompt has no unfilled placeholders")

d = tempfile.mkdtemp()
Path(d, "orchestrator.md").write_text("VARIANT orchestrator\n")
reg.load_all.cache_clear()
os.environ["RC_AGENT_PROMPTS"] = d
v = reg.load_all()
check(v["orchestrator"].text.startswith("VARIANT") and v["incident"].text == ALL["incident"].text
      and v["orchestrator"].version != ALL["orchestrator"].version, "a variant directory overrides only what it has")
del os.environ["RC_AGENT_PROMPTS"]
reg.load_all.cache_clear()

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
