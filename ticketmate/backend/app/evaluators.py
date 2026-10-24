"""Evaluators run on every draft -- LangSmith's online evaluation, kept local.

Three kinds, each recorded as feedback on the draft's trace:

  assertion  code checks of the rules the system prompt states (sign-off,
             no placeholder names, no invented ticket numbers, no promised
             timelines, ...) and of structure. Score 1 = passed, 0 = failed.
             Instant; also returned to the UI so a failure is visible.
  metric     measured without an LLM: conciseness (reply length against a
             target), answer relevance (embedding similarity of ticket and
             reply, via rc_kg's local model), hallucination signals (invented
             URLs, unsupported citations).
  llm_judge  hallucination, correctness, conciseness and answer relevance
             judged by the serving model, in the background after the reply
             is returned -- it never delays a draft. The judge is the same
             model that wrote the draft, so treat its scores as a trend
             signal, not ground truth; people's feedback is the ground truth.

Direction matters: for `hallucination` and `hallucinated_urls` lower is
better; for everything else higher is better. Each record says which.
"""

import asyncio
import json
import logging
import random
import re
from typing import Any, Dict, List, Optional

from . import kg_client, runlog
from .config import get_settings

logger = logging.getLogger(__name__)

SIGN_OFF = "Best regards,\nResearch Computing"

_PLACEHOLDER = re.compile(r"\[(?:your |insert |the )?(?:name|first name|engineer|signature)[^\]]*\]|<\s*your name\s*>|\bXXXX+\b", re.I)
_TICKET_ID = re.compile(r"\b(?:SCTASK|RITM|INC|TASK|CHG|PRB|REQ|CS)\d{5,}\b", re.I)
_PROMISE = re.compile(
    r"\b(?:within \d+\s*(?:business\s+)?(?:hours?|days?|weeks?)|by (?:tomorrow|end of (?:the )?(?:day|week))"
    r"|we (?:will|'ll) (?:increase|extend|grant|approve|raise) (?:your|the)"
    r"|i (?:will|'ll) escalate|we guarantee|guaranteed)\b", re.I)
_MISSING = re.compile(r"\b(?:not (?:provided|given|specified|included|mentioned)|missing|unclear|unknown"
                      r"|no (?:job id|error message|username))\b", re.I)

# Conciseness: replies up to TARGET words score 1, falling linearly to 0 at
# LIMIT. Body only -- the References block and sign-off do not count. These
# are starting points to tune against what engineers actually send.
CONCISE_TARGET_WORDS = 180
CONCISE_LIMIT_WORDS = 450


def _reply_body(reply: str) -> str:
    body = reply.split("\n\nReferences:\n", 1)[0]
    return body.replace(SIGN_OFF, "").strip()


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def assertions(response: Dict[str, Any], ticket_text: str, kg_status: str, kg_low_confidence: bool,
               cite_trace: Optional[Dict[str, Any]]) -> Dict[str, bool]:
    reply = response.get("draft_response") or ""
    caveats = response.get("caveats") or []
    allowed_ids = {m.upper() for m in _TICKET_ID.findall(ticket_text)}
    cite = cite_trace or {}
    checks = {
        # rules stated in the system prompt
        "assert_signoff": reply.rstrip().endswith(SIGN_OFF),
        "assert_no_placeholder": not _PLACEHOLDER.search(reply),
        "assert_no_invented_ticket_number": {m.upper() for m in _TICKET_ID.findall(reply)} <= allowed_ids,
        "assert_no_promises": not _PROMISE.search(reply),
        "assert_confidence_consistent": not ((response.get("confidence") or 0) >= 0.8
                                             and any(_MISSING.search(c) for c in caveats)),
        "assert_summary_short": _sentences(response.get("problem_summary") or "") <= 2,
        # structure and grounding
        "assert_has_steps": bool(response.get("suggested_steps")),
        "assert_urls_from_sources": not cite.get("removed_urls"),
        "assert_no_unknown_citations": not cite.get("unknown_refs"),
    }
    if kg_status == "ok" and not kg_low_confidence:
        checks["assert_cites_when_grounded"] = bool(response.get("references"))
    return checks


def metrics(response: Dict[str, Any], cite_trace: Optional[Dict[str, Any]]) -> Dict[str, float]:
    body = _reply_body(response.get("draft_response") or "")
    words = len(body.split())
    span = CONCISE_LIMIT_WORDS - CONCISE_TARGET_WORDS
    conciseness = 1.0 if words <= CONCISE_TARGET_WORDS else max(0.0, 1 - (words - CONCISE_TARGET_WORDS) / span)
    support = (cite_trace or {}).get("support") or []
    out = {"reply_words": float(words), "conciseness": round(conciseness, 3),
           "hallucinated_urls": float(len((cite_trace or {}).get("removed_urls") or []))}
    if support:
        out["unsupported_citation_rate"] = round(sum(1 for v in support if v["verdict"] != "ok") / len(support), 3)
    return out


async def answer_relevance(ticket_text: str, reply: str, request_id: str) -> Optional[float]:
    """Embedding similarity of the ticket (as a query) to the reply body (as
    a passage), with the same local model retrieval uses. None without rc_kg."""
    body = _reply_body(reply)
    if not body:
        return None
    scores = await kg_client.similarity(ticket_text, [body], request_id=request_id)
    return scores[0] if scores else None


def lower_is_better(key: str) -> bool:
    return key in ("hallucination", "hallucinated_urls", "unsupported_citation_rate", "reply_words")


# ── LLM judge (background) ──────────────────────────────────────────────────
JUDGE_SYSTEM = """\
You are a strict evaluator of draft replies written by an assistant for a university \
Research Computing (HPC) help desk. You are given the researcher's ticket, the RC \
documentation sources the assistant was given, and the assistant's draft (its reply and \
its suggested steps). Judge only what is in front of you.

Return ONLY a JSON object with exactly these keys:
{"hallucination": {"score": <0..1>, "unsupported_claims": [<up to 5 short quotes>]},
 "correctness": {"score": <0..1>, "reason": "<one sentence>"},
 "conciseness": {"score": <0..1>, "reason": "<one sentence>"},
 "answer_relevance": {"score": <0..1>, "reason": "<one sentence>"}}

- hallucination: the share of the draft's factual claims (commands, limits, paths, \
policies, names) that are supported by NEITHER the sources NOR the ticket. 0 = every \
claim is supported; 1 = none is. General advice that needs no source is not a claim.
- correctness: would following this draft resolve the researcher's actual problem, \
given the sources? 1 = yes; 0.5 = partly or with a gap; 0 = no or wrong.
- conciseness: 1 = says what is needed without padding or repetition; 0 = very verbose.
- answer_relevance: 1 = addresses exactly what the ticket asks; 0 = off-topic.
"""


def _judge_user(ticket: str, context: str, draft: Dict[str, Any]) -> str:
    steps = "\n".join("- " + s for s in draft.get("suggested_steps") or [])
    return ("--- TICKET ---\n%s\n\n--- SOURCES ---\n%s\n\n--- DRAFT REPLY ---\n%s\n\n--- SUGGESTED STEPS ---\n%s\n\n"
            "Return the JSON object only." % (ticket.strip(), context.strip() or "(none)",
                                              _reply_body(draft.get("draft_response") or ""), steps))


def _clip01(x: Any) -> Optional[float]:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


async def judge_and_record(request_id: str, ticket_text: str, kg_context: str, draft: Dict[str, Any]) -> None:
    """Ask the serving model to grade the draft; append each grade as
    llm_judge feedback beside the run. Errors are recorded, never raised."""
    import time
    from .llm_client import _usage, extract_json, get_client
    settings = get_settings()
    t0 = time.perf_counter()
    client = get_client(settings)
    meta = {"judge_model": settings.vllm_model_name}
    try:
        kwargs = dict(model=settings.vllm_model_name, temperature=0.0, max_tokens=600,
                      messages=[{"role": "system", "content": JUDGE_SYSTEM},
                                {"role": "user", "content": _judge_user(ticket_text, kg_context, draft)}])
        try:
            completion = await client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
        except Exception:                          # noqa: BLE001 -- server without JSON mode
            completion = await client.chat.completions.create(**kwargs)
        meta.update(latency_ms=int((time.perf_counter() - t0) * 1000), usage=_usage(completion))
        verdict = extract_json(completion.choices[0].message.content or "")
    except Exception as exc:                       # noqa: BLE001 -- evaluation must never break drafting
        runlog.add_feedback(request_id, {"key": "llm_judge_error", "score": 0, "source": "llm_judge",
                                         "comment": str(exc)[:300], "metadata": meta})
        logger.warning("judge failed | id=%s %s", request_id, str(exc)[:200])
        return
    finally:
        await client.close()
    for key in ("hallucination", "correctness", "conciseness", "answer_relevance"):
        item = verdict.get(key) if isinstance(verdict, dict) else None
        score = _clip01((item or {}).get("score")) if isinstance(item, dict) else None
        if score is None:
            continue
        comment = (item.get("reason") or "; ".join(item.get("unsupported_claims") or []))[:500]
        runlog.add_feedback(request_id, {"key": key, "score": score, "source": "llm_judge",
                                         "comment": comment, "lower_is_better": lower_is_better(key),
                                         "metadata": meta})
    logger.info("judge ok | id=%s %s", request_id,
                " ".join("%s=%s" % (k, (verdict.get(k) or {}).get("score")) for k in
                         ("hallucination", "correctness", "conciseness", "answer_relevance")
                         if isinstance(verdict.get(k), dict)))


_tasks: set = set()


def schedule_judge(request_id: str, ticket_text: str, kg_context: str, draft: Dict[str, Any]) -> bool:
    settings = get_settings()
    if not settings.online_eval or random.random() >= settings.online_eval_sample_rate:
        return False
    task = asyncio.create_task(judge_and_record(request_id, ticket_text, kg_context, draft))
    _tasks.add(task)                               # keep a reference until it finishes
    task.add_done_callback(_tasks.discard)
    return True
