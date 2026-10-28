"""The reviewer agent: checks every factual claim in a draft against the
evidence it was written from, before an RC engineer sees it.

The writer drafts from the ticket, the cluster lookups, the knowledge-graph
sources and the skills. The reviewer gets exactly those, plus the draft, and
lists the claims none of them supports (a node name, a limit, "we have added
you", a time frame) and anything else wrong (contradicts an RC staff entry,
answers a different question, asks for something already given). Its
findings, with the rule-based critic's, become the writer's revision notes.

A separate model call with its own prompt (prompts/reviewer.md), so the
writer's reasoning does not leak into the verdict; guided JSON where the
server has it, parsed leniently where it does not. If the reviewer fails or
returns nonsense, the draft goes through un-reviewed and the trace says so:
review must never block a draft.
"""

import logging
from typing import Dict, List

from .. import llm_client, lookups, tracing
from ..llm_client import extract_json
from . import prompt_registry as prompts
from .state import Evidence, Review

logger = logging.getLogger(__name__)

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
        "problems": {"type": "array", "items": {"type": "string"}},
        "supported_share": {"type": "number"},
    },
    "required": ["unsupported_claims", "problems", "supported_share"],
    "additionalProperties": False,
}
MAX_EVIDENCE_CHARS = 6000


def _clip(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n[...]"


async def review(draft: Dict, ticket: str, evidence: Evidence) -> Review:
    """The reviewer's verdict on `draft`. Never raises."""
    user = prompts.get("reviewer_user").render(
        ticket=_clip(ticket, 5000),
        lookups=_clip(lookups.prompt_text(evidence.lookups) or "(none)", 2500),
        sources=_clip(evidence.kg.context if evidence.kg and evidence.kg.status == "ok" else "(none)",
                      MAX_EVIDENCE_CHARS),
        skills=", ".join(m.playbook.title for m in evidence.skills) or "(none)",
        summary=draft.get("problem_summary", ""),
        steps="\n".join("- " + x for x in draft.get("suggested_steps") or []) or "(none)",
        reply=draft.get("draft_response", ""))
    msgs = [{"role": "system", "content": prompts.get("reviewer").text}, {"role": "user", "content": user}]
    with tracing.run("review.reviewer", "chain", metadata={"prompt": prompts.get("reviewer").version}) as run:
        try:
            out = await llm_client.chat(msgs, None, max_tokens=500, name="agent.reviewer",
                                        json_schema=REVIEW_SCHEMA)
            data = extract_json(out["content"])
            claims = [str(x)[:300] for x in data.get("unsupported_claims") or [] if str(x).strip()][:8]
            probs = [str(x)[:300] for x in data.get("problems") or [] if str(x).strip()][:6]
            share = data.get("supported_share")
            share = float(share) if isinstance(share, (int, float)) and 0 <= share <= 1 else None
            notes: List[str] = []
            if claims:
                notes.append("These claims are not supported by the ticket, the lookups or the sources -- remove "
                             "them, or say they need to be checked: " + "; ".join('"%s"' % c for c in claims))
            notes += probs
            verdict = Review("reviewer", notes, unsupported_claims=claims, score=share)
        except Exception as exc:                       # noqa: BLE001 -- review never blocks a draft
            logger.warning("reviewer failed: %s", exc)
            verdict = Review("reviewer", [], detail="not reviewed: %s" % str(getattr(exc, "message", exc))[:200])
        if run is not None:
            run.end(verdict.to_dict(), error=verdict.detail or None)
    return verdict
