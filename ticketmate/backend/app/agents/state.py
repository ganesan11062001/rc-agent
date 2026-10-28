"""The state that flows through the agent graph, and the records it is made of.

One `GraphState` per draft. LangGraph merges what each node returns into it;
list fields marked `Annotated[..., operator.add]` accumulate (every node
appends to `path`, every tool call to `tool_calls`), the rest are replaced.
Everything here is plain data or one of the app's own dataclasses, so the
whole state can be written to the run log at the end -- the draft's trace is
the state's history.
"""

import operator
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict

from ..kg_client import KGContext
from ..lookups import Lookup
from ..playbooks import Match

Route = Literal["incident", "request", "followup"]


@dataclass
class Evidence:
    """What the reply may be written from: lookups that ran, knowledge-graph
    sources, and loaded skills. Agents only add to it."""
    lookups: List[Lookup] = field(default_factory=list)
    kg: Optional[KGContext] = None
    skills: List[Match] = field(default_factory=list)

    def summary(self) -> Dict[str, Any]:
        return {"lookups": [{"command": lk.command, "ok": lk.ok, "summary": lk.summary} for lk in self.lookups],
                "sources": [{"ref": s.get("ref"), "title": "%s > %s" % (s.get("page_title"), s.get("heading")),
                             "url": s.get("url")} for s in (self.kg.sources if self.kg else [])],
                "low_confidence": bool(self.kg and self.kg.low_confidence),
                "skills": [m.to_dict() for m in self.skills]}


@dataclass
class ToolCallRecord:
    """One tool call by one agent: what was asked, whether it ran, what came back."""
    agent: str
    step: int
    tool: str
    arguments: Dict[str, Any]
    ok: bool
    result: str                     # what the model read back (capped)
    latency_ms: int = 0
    cached: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Review:
    """A verdict on a draft: the rule-based critic's and the reviewer agent's."""
    reviewer: str                   # "critic" | "reviewer"
    problems: List[str]
    unsupported_claims: List[str] = field(default_factory=list)
    score: Optional[float] = None   # reviewer: share of claims supported, 0-1
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_dict(self) -> Dict[str, Any]:
        return dict(asdict(self), ok=self.ok)


class GraphState(TypedDict, total=False):
    # Inputs (set once).
    request_id: str
    ticket: str                      # the cleaned ticket, entries labelled RC staff / researcher
    query: str                       # the researcher's own words (ticket_context.Prepared.query)
    instructions: str                # what the RC engineer typed
    ticket_number: str
    # Routing.
    route: Route
    route_reason: str
    # Investigation.
    evidence: Evidence
    plan: str
    reflections: Annotated[List[str], operator.add]
    tool_calls: Annotated[List[ToolCallRecord], operator.add]
    agent_stopped: str               # done | step budget | call budget | tools unsupported | error
    agent_steps: int
    # Writing and review.
    draft: Dict[str, Any]            # LLMDraft as a dict
    model: str
    reviews: Annotated[List[Review], operator.add]
    revisions: int
    # Provenance: every node appends its name, in order.
    path: Annotated[List[str], operator.add]
    usage: Annotated[List[Dict[str, int]], operator.add]
