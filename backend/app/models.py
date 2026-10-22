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

    ticket_text: str = Field(..., min_length=1, max_length=50_000)

    # Optional free-text guidance from the RC member: context the ticket does
    # not contain, or a steer on tone/length.
    extra_instructions: str = Field(default="", max_length=10_000)

    # Optional, for logging and for the capture build's file layout.
    ticket_number: str = Field(default="", max_length=64)


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

    @classmethod
    def from_llm_draft(
        cls, draft: LLMDraft, model: str, latency_ms: int
    ) -> "DraftResponse":
        return cls(
            **draft.model_dump(),
            confidence_label=confidence_label(draft.confidence),
            model=model,
            latency_ms=latency_ms,
        )


class LoginRequest(BaseModel):
    access_token: str = Field(..., min_length=1, max_length=512)


class LoginResponse(BaseModel):
    session_token: str
    expires_at: int  # unix seconds, enforced server-side
    session_hours: float


class HealthResponse(BaseModel):
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
