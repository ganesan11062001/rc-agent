"""Skills: RC's procedures, as the agents see them.

A skill is a playbook file (app/playbooks/*.md -- the format is documented in
app/playbooks.py): a runbook with stages, reply templates and the
information it needs, plus its triggers. Two kinds:

- "procedure": a kind of request (aicr-access, multigpu-access, ...),
  triggered by the researcher's own words;
- "reply": a kind of reply the engineer asked for (follow-up-closure),
  triggered by the engineer's instructions.

Skills reach a draft two ways, and the trace says which: `trigger` -- the
rules matched before any agent ran -- or `load` -- an agent decided the
ticket needs one (the `load_skill` tool). Either way the writer gets the
full runbook, the values found in the ticket and what is still missing.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional

from .. import playbooks
from ..playbooks import Match, Playbook


@dataclass(frozen=True)
class SkillInfo:
    """A skill's card, as the trace and the agents' tool description show it."""
    name: str
    title: str
    kind: str                 # "procedure" | "reply"
    triggers: Dict[str, List[str]]
    requires: List[str]

    def to_dict(self) -> Dict:
        return {"name": self.name, "title": self.title, "kind": self.kind,
                "triggers": self.triggers, "requires": self.requires}


def _info(b: Playbook) -> SkillInfo:
    return SkillInfo(
        name=b.name, title=b.title, kind="reply" if b.on == "instructions" else "procedure",
        triggers={"reads": ["engineer instructions" if b.on == "instructions" else "researcher's words"],
                  "match": [p.pattern for p in b.match], "unless": [p.pattern for p in b.unless]},
        requires=[ask for _, ask in b.requires])


def catalog() -> List[SkillInfo]:
    """Every skill, for the trace and the load_skill tool."""
    return [_info(b) for b in playbooks.load()]


def procedures() -> List[SkillInfo]:
    return [s for s in catalog() if s.kind == "procedure"]


def trigger(query: str, ticket: str, instructions: str = "") -> List[Match]:
    """The skills whose triggers match this ticket (before any agent runs)."""
    return playbooks.find(query, ticket, instructions=instructions)


def load(name: str, ticket: str) -> Optional[Match]:
    """One skill by name, with the values found in `ticket`; None if no such skill."""
    book = next((b for b in playbooks.load() if b.name == name), None)
    return Match(book, book.extract(ticket)) if book else None
