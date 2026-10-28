"""The critic: checks a draft for the mistakes RC's reviewers found, before
anyone sees it.

Deterministic on purpose. The in-loop LLM judge (the same 7B model grading
itself) gave correctness 1.0 to drafts the team marked as failures; these
checks are the failures they named, written as rules:

- claims an action was done that nothing in the thread or the lookups shows
  (INC09000015: "we have granted you access")
- sends the researcher to RC / rchelp / the systems team to solve the
  problem (INC09000008), outside a follow-up or closing message
- repeats RC's previous reply instead of writing the next one
- does not ask for what the playbook says is missing
- a placeholder or a personal sign-off instead of "Research Computing"

When something is found, the writer drafts once more with the findings as
revision notes (app/agents/graph.py).
"""

import re
from typing import Dict, List

# "we have added you", "I've granted", "has been created", "is now resolved"
_DONE = re.compile(
    r"(?i)\b(?:we|i)(?:'ve| have)\s+(?:now\s+|already\s+)?(added|granted|created|increased|extended|"
    r"fixed|restored|resolved|provisioned|enabled|approved|removed|reset)\b|"
    r"\b(?:has|have)\s+been\s+(added|granted|created|increased|extended|fixed|restored|resolved|"
    r"provisioned|enabled|approved|removed|reset)\b")
_SEND_TO_RC = re.compile(
    r"(?i)\b(?:contact|reach out to|email|e-mail|write to|get in touch with)\s+(?:the\s+)?"
    r"(?:research computing|rc|rchelp|systems? team|sysadmins?|hpc team)\b")
_PLACEHOLDER = re.compile(r"\[(?:your name|name|insert[^\]]*|username|job ?id)\]|<your name>", re.I)
_STAFF_ENTRY = re.compile(r"^\[[^\]]+\] [^\n]*, RC staff \([^)]*\):\n(.*?)(?=^\[[^\]]+\] |\Z)", re.M | re.S)
_WORD = re.compile(r"[a-z0-9]+")


def _shingles(text: str, n: int = 5) -> set:
    words = _WORD.findall(text.lower())
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def review(draft: Dict, ticket_text: str, lookups_text: str = "", missing: List[str] = (),
           closing: bool = False) -> List[str]:
    """The problems found in `draft` (an LLMDraft dict), as revision notes; [] if none."""
    reply = draft.get("draft_response") or ""
    staff = [m.group(1) for m in _STAFF_ENTRY.finditer(ticket_text)]
    evidence = (" ".join(staff) + " " + lookups_text).lower()
    problems: List[str] = []

    for m in _DONE.finditer(reply):
        verb = (m.group(1) or m.group(2) or "").lower()
        stem = verb[:-2] if verb.endswith("ed") else verb
        if stem[:5] not in evidence:
            problems.append(
                'The reply says something was %s ("%s"), but no RC staff entry or lookup shows it. '
                "Say what will happen instead, and put the action in the steps." % (verb, m.group(0)))
            break

    if not closing:
        m = _SEND_TO_RC.search(reply)
        if m:
            problems.append('The reply tells the researcher to "%s" -- you are Research Computing. '
                            "Say what RC is doing about it instead." % m.group(0))

    last_staff_comment = next((s for s in reversed(staff) if s.strip()), "")
    if last_staff_comment and len(reply) > 200:
        a, b = _shingles(reply), _shingles(last_staff_comment)
        if a and b and len(a & b) / len(a) > 0.5:
            problems.append("The reply repeats RC's previous message. Write the next message in the "
                            "thread: only what is new.")

    if missing and reply:
        asked = "?" in reply or re.search(r"(?i)\b(please (send|share|provide|confirm|let us know)|could you)\b", reply)
        if not asked:
            problems.append("The ticket does not give: %s. Unless it gives them some other way, the "
                            "reply must ask the researcher for them." % "; ".join(missing))

    if _PLACEHOLDER.search(reply) or not re.search(r"Best regards,\s*\n\s*Research Computing\s*$", reply.strip()):
        problems.append('End the reply with exactly "Best regards,\\nResearch Computing", with no '
                        "placeholder such as [Your Name] anywhere.")
    return problems
