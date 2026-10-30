"""Pydantic models for the API surface and for the LLM's structured output."""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

# Confidence thresholds shared by the backend and (mirrored in) the frontend.
HIGH_CONFIDENCE = 0.8
MEDIUM_CONFIDENCE = 0.6


def confidence_label(score: float) -> str:
    """>=0.8 high, 0.6-0.79 medium, <0.6 low."""
    if score >= HIGH_CONFIDENCE:
        return "high"
    if score >= MEDIUM_CONFIDENCE:
        return "medium"
    return "low"


class DraftRequest(BaseModel):
    """What the frontend POSTs to /api/draft."""

    # Raw page text, so it can be long: the backend cleans it and fits it to
    # the model's context window (app/ticket_context.py).
    ticket_text: str = Field(..., min_length=1, max_length=200_000)

    # Optional free-text guidance from the RC member: context the ticket does
    # not contain, or a steer on tone/length.
    extra_instructions: str = Field(default="", max_length=10_000)

    # Optional, for logging and for the capture build's file layout.
    ticket_number: str = Field(default="", max_length=64)

    # The engineer's own sign-off (the extension's settings), used exactly as
    # written in place of the default "Best regards,\nResearch Computing".
    signoff: str = Field(default="", max_length=300)


class LLMDraft(BaseModel):
    """Exactly the shape we ask the model to emit.

    `confidence_label` is deliberately absent: we derive it server-side from
    `confidence` so the thresholds are enforced by us, not by the model.
    """

    problem_summary: str
    suggested_steps: List[str]
    confidence: float = Field(..., ge=0.0, le=1.0)
    caveats: List[str]
    draft_response: str


class Reference(BaseModel):
    """One RC website page cited in the draft, numbered as in the reply."""

    n: int
    title: str
    url: str


class ClusterLookup(BaseModel):
    """One read-only command run for this draft (app/lookups.py)."""

    kind: str          # "user" | "job" | "group" | "node" | "partition"
    target: str
    command: str       # as shown, e.g. "id doe.l"
    ok: bool
    summary: str
    output: str = ""
    ms: int = 0


class DraftResponse(LLMDraft):
    """What /api/draft returns: the model's draft plus derived fields."""

    confidence_label: str
    model: str
    latency_ms: int
    # RC website pages the draft cites (already written into draft_response as
    # a References block), and whether grounding was used at all:
    # "ok" | "unavailable" | "disabled".
    references: List[Reference] = Field(default_factory=list)
    kg_status: str = "disabled"
    # What the ticket became before the model read it: chars before/after,
    # notifications, addresses and signatures removed, entries kept/omitted.
    context: Dict[str, Any] = Field(default_factory=dict)
    # Read-only cluster lookups that ran for this draft, and fed its prompt.
    lookups: List[ClusterLookup] = Field(default_factory=list)
    # Traceability: the id of this draft's run log (and of rc_kg's log lines
    # for it), and the tokens it cost across every model call it took.
    request_id: str = ""
    usage: Optional[Dict[str, int]] = None
    # Evaluator results at draft time: assertions (pass/fail) and metrics.
    evaluations: Dict[str, Any] = Field(default_factory=dict)
    # The LLM judge's grades (JUDGE_MODE=gate): {mode, verdict "ok"|"low"|
    # "error", scores {key: {score, comment}}, rubric [{item, met, reason}],
    # reasons, rewritten, first (the first draft's grades when rewritten)}.
    judge: Dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_llm_draft(
        cls, draft: LLMDraft, model: str, latency_ms: int
    ) -> "DraftResponse":
        """Wrap the model's draft as a response, deriving confidence_label server-side."""
        return cls(
            **draft.model_dump(),
            confidence_label=confidence_label(draft.confidence),
            model=model,
            latency_ms=latency_ms,
        )


class FeedbackRequest(BaseModel):
    """A person's judgement of one draft, by its request_id."""

    request_id: str = Field(..., pattern=r"^[0-9a-f]{8}$")
    # e.g. "thumbs" (1 / 0), "correctness" (0..1), "sent_as_is" (1 / 0)
    key: str = Field(..., pattern=r"^[a-z][a-z0-9_]{0,39}$")
    # None for comment-only feedback.
    score: Optional[float] = Field(default=None, ge=-1, le=1)
    # A categorical answer, e.g. usage: "used_as_is" | "edited" | "not_used".
    value: str = Field(default="", max_length=40)
    comment: str = Field(default="", max_length=2000)
    # The reply as the engineer actually sent it, if they changed it.
    correction: str = Field(default="", max_length=50_000)


class FeedbackResponse(BaseModel):
    """What /api/feedback returns: the stored feedback's id and its draft's request_id."""
    feedback_id: str
    request_id: str


class LoginRequest(BaseModel):
    """What the frontend POSTs to /api/login: the access token printed at startup."""
    access_token: str = Field(..., min_length=1, max_length=512)


class LoginResponse(BaseModel):
    """What /api/login returns: a session token for the Authorization header."""
    session_token: str
    expires_at: int  # unix seconds, enforced server-side
    session_hours: float


class HealthResponse(BaseModel):
    """What /api/health returns: model server, knowledge graph and startup status."""
    status: str  # "ok" | "unreachable"
    auth_required: bool = True
    vllm_base_url: str
    configured_model: str
    model_available: Optional[bool] = None
    available_models: List[str] = Field(default_factory=list)
    detail: Optional[str] = None
    # "ok" | "loading" | "unreachable" | "incompatible" | "disabled" -- separate from `status`,
    # which stays about the model so existing clients read it unchanged.
    knowledge_graph: str = "disabled"
    knowledge_graph_detail: Optional[str] = None
    # Startup progress of vLLM and rc_kg (app/startup.py), for the progress
    # display while a fresh session loads: {ready, model_ready, elapsed_s,
    # vllm: {stage, label, pct, detail, failed}, knowledge_graph: {..., steps}}.
    startup: Optional[Dict[str, Any]] = None


# --- JSON schema handed to the server for guided decoding -----------------
# Hand-written rather than generated from LLMDraft so it satisfies OpenAI's
# "strict" json_schema rules (no extra keys, everything required) and stays
# small enough for vLLM's grammar compiler to be happy with it.
DRAFT_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "problem_summary",
        "suggested_steps",
        "confidence",
        "caveats",
        "draft_response",
    ],
    "properties": {
        "problem_summary": {
            "type": "string",
            "description": "One or two sentences describing the researcher's actual problem.",
        },
        "suggested_steps": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Concrete troubleshooting/resolution steps for the RC engineer.",
        },
        "confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": "How confident you are that this draft is correct and complete.",
        },
        "caveats": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Things the RC member must double check before sending.",
        },
        "draft_response": {
            "type": "string",
            "description": "Ready-to-paste reply to the researcher.",
        },
    },
}
