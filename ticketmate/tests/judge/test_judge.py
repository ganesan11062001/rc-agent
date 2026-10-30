"""The engineer's sign-off, the LLM judge's decisions and the readable trace
(app/evaluators.py, app/trace_report.py). No model needed.

    backend/.venv/bin/python tests/judge/test_judge.py    exit 0 = pass
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app import evaluators as E, trace_report  # noqa: E402

fails = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


print("sign-off")
check(E.signoff_text("") == E.SIGN_OFF and E.signoff_text("   \n ") == E.SIGN_OFF, "empty: the team sign-off")
custom = E.signoff_text("Thanks,\r\nSam Rivera  \r\nResearch Computing\n")
check(custom == "Thanks,\nSam Rivera\nResearch Computing", "tidied, exact otherwise: %r" % custom)
check(E.signoff_text("\n".join("l%d" % i for i in range(10))).count("\n") == E.MAX_SIGNOFF_LINES - 1, "at most 6 lines")
reply = "Hi,\n\nText [1].\n\nReferences:\n[1] Page\n\nBest regards,\nResearch Computing"
out = E.apply_signoff(reply, custom)
check(out.endswith(custom) and "Best regards" not in out and out.index("References:") < out.index(custom),
      "replaces the default where the model wrote it; References stay above")
check(E.apply_signoff("no sign-off here", custom) == "no sign-off here", "nothing to replace: unchanged")
a = E.assertions({"draft_response": out, "suggested_steps": ["x"]}, "", "disabled", False, None, signoff=custom)
check(a["assert_signoff"], "the sign-off check uses the engineer's sign-off")
check(not E.assertions({"draft_response": reply, "suggested_steps": ["x"]}, "", "disabled", False, None,
                       signoff=custom)["assert_signoff"], "and fails when the reply ends otherwise")

print("judge decisions")
ok = {"scores": {"hallucination": {"score": 0.1, "comment": ""}, "correctness": {"score": 0.9, "comment": ""}},
      "rubric": [{"item": "asks for a benchmark", "met": True, "reason": ""}]}
check(E.flags(ok) == [], "within thresholds, rubric met: not flagged")
bad = {"scores": {"hallucination": {"score": 0.6, "comment": "made-up limit"}, "correctness": {"score": 0.2, "comment": "wrong"}},
       "rubric": [{"item": "asks for a benchmark", "met": False, "reason": "it grants access"},
                  {"item": "a stage it is not at", "met": None, "reason": ""}]}
fl = E.flags(bad)
check(len(fl) == 3 and fl[0].startswith("unsupported claims (60%): made-up limit") and "wrong" in fl[1]
      and fl[2] == "not done: asks for a benchmark (it grants access)", "hallucination, correctness, unmet rubric: %s" % fl)
ok["reasons"], bad["reasons"] = E.flags(ok), fl
check(E.better(bad, ok) and not E.better(ok, bad), "fewer flags is better")
check("REVIEWER" in E.revision_note(bad) and "- not done: asks for a benchmark" in E.revision_note(bad),
      "the rewrite is told exactly what to fix")
check(E.caveat(bad).startswith("Quality check (LLM judge)"), "the caveat names the judge")
user = E._judge_user("ticket", "", {"draft_response": out}, ["asks for a benchmark"])
check("--- RUBRIC ---\n- asks for a benchmark" in user and custom not in user, "the judge sees the rubric, not the sign-off")

print("readable trace")
run = {"request_id": "ab12cd34", "ticket_number": "INC0000042", "at": "2026-10-30T10:00:00-04:00",
       "model": "stub", "strategy": "json_schema", "latency_ms": 900, "input": {"extra_instructions": "be brief"},
       "prompt": [{"role": "system", "content": "rules"}, {"role": "user", "content": "the | ticket"}],
       "raw_response": "{}",
       "draft": {"draft_response": out, "problem_summary": "OOM.", "suggested_steps": ["raise --mem"],
                 "caveats": ["check the job id"], "confidence": 0.8, "confidence_label": "high",
                 "evaluations": {"assertions": {"assert_signoff": True}, "assertions_failed": [], "metrics": {"conciseness": 1.0}},
                 "judge": {"mode": "gate", "verdict": "ok", "rewritten": True, "rewrite": "kept",
                           "first": {"verdict": "low", "reasons": ["wrong"]},
                           "scores": {"correctness": {"score": 0.9, "comment": "fine"}},
                           "rubric": [{"item": "asks for a benchmark", "met": True, "reason": "yes"}]}},
       "trace": {"tokens": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2, "model_calls": 2},
                 "llm": {"attempts": [{"n": 1, "strategy": "json_schema", "outcome": "ok"},
                                      {"n": 2, "phase": "rewrite", "strategy": "json_schema", "outcome": "ok"}]},
                 "playbooks": [{"title": "Example", "values": {"username": ["doe.l"]},
                                "value_sources": {"username": "form: Username"}, "missing": []}],
                 "run_tree": {"runs": [{"id": "r", "parent_id": None, "name": "draft", "run_type": "chain",
                                        "latency_ms": 900, "status": "success"},
                                       {"id": "j", "parent_id": "r", "name": "llm_judge", "run_type": "llm",
                                        "latency_ms": 100, "status": "success"}]}}}
md = trace_report.render(run, [{"key": "thumbs", "score": 1, "source": "user", "by": "s.rivera"}])
for want in ["# Draft ab12cd34 -- INC0000042", "## The reply", "Sam Rivera", "**Steps**", "check the job id",
             "| signoff | PASS |", "LLM judge** (gate mode, ok)", "| asks for a benchmark | yes | yes |",
             "rewrite: kept", "username = doe.l (form: Username)", "| 2 | rewrite |", "  - `llm_judge` (llm)",
             "| thumbs |", "be brief", "<details><summary>user message"]:
    check(want in md, "the page has %r" % want)
check(trace_report.render({}) .startswith("# Draft ?"), "an empty or old log still renders")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
