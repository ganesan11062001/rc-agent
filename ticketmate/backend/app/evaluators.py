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
             judged by the serving model, plus the matched request type's
             rubric (each playbook's `rubric` lines and what it still needs:
             a multi-GPU reply is graded on what a multi-GPU reply must do).
             JUDGE_MODE=gate (default) grades before the draft is returned,
             so the panel shows it and a flagged draft is rewritten once
             (app/main.py); "background" grades after, without delaying it.
             The judge is the same model that wrote the draft, so treat its
             scores as a strong hint, not ground truth; people's feedback is
             the ground truth.

Direction matters: for `hallucination`, `hallucinated_urls`,
`unsupported_citation_rate` and `reply_words` lower is better; for everything
else higher is better (see lower_is_better). Each llm_judge record says which.
"""

import asyncio
import logging
import random
import re
from typing import Any, Dict, List, Optional

from . import kg_client, runlog
from .config import get_settings

logger = logging.getLogger(__name__)

SIGN_OFF = "Best regards,\nResearch Computing"     # the default; an engineer may set their own
MAX_SIGNOFF_LINES = 6


def signoff_text(custom: str) -> str:
    """The sign-off a reply ends with: the engineer's own (from the extension's
    settings), tidied, else the default. Used exactly as written."""
    lines = [ln.rstrip() for ln in (custom or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    text = "\n".join(lines).strip()
    return "\n".join(text.split("\n")[:MAX_SIGNOFF_LINES]) if text else SIGN_OFF


def apply_signoff(reply: str, signoff: str) -> str:
    """Put the engineer's sign-off where the model wrote the default one.

    The model is always asked for the default (a 7B model copies a fixed
    string reliably; a name it might "improve"); code swaps in the engineer's
    own, exactly as typed. The References block stays before it."""
    if signoff == SIGN_OFF or SIGN_OFF not in reply:
        return reply
    head, _, tail = reply.rpartition(SIGN_OFF)
    return head + signoff + tail

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


def _reply_body(reply: str, signoff: str = SIGN_OFF) -> str:
    body = reply.split("\n\nReferences:\n", 1)[0]
    return body.replace(signoff, "").replace(SIGN_OFF, "").strip()


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s])


def assertions(response: Dict[str, Any], ticket_text: str, kg_status: str, kg_low_confidence: bool,
               cite_trace: Optional[Dict[str, Any]], instructed: bool = False,
               signoff: str = SIGN_OFF) -> Dict[str, bool]:
    """Code checks of the system prompt's rules and of the draft's structure.

    Returns {assert_*: passed}. `cite_trace` is apply_citations' trace (None
    when there was no grounding); `instructed` means the engineer gave
    instructions, which turns off the must-cite check; `signoff` is the
    sign-off the reply must end with (signoff_text).
    """
    reply = response.get("draft_response") or ""
    caveats = response.get("caveats") or []
    allowed_ids = {m.upper() for m in _TICKET_ID.findall(ticket_text)}
    cite = cite_trace or {}
    checks = {
        # rules stated in the system prompt
        "assert_signoff": reply.rstrip().endswith(signoff.rstrip()),
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
    # Not when the engineer said what the reply is: a follow-up or closure
    # note has nothing to cite, and the instructions outrank the default shape.
    if kg_status == "ok" and not kg_low_confidence and not instructed:
        checks["assert_cites_when_grounded"] = bool(response.get("references"))
    return checks


def metrics(response: Dict[str, Any], cite_trace: Optional[Dict[str, Any]],
            signoff: str = SIGN_OFF) -> Dict[str, float]:
    """LLM-free measures of the draft: reply length, conciseness, invented URLs and,
    when citations were checked, the share of unsupported ones.
    """
    body = _reply_body(response.get("draft_response") or "", signoff)
    words = len(body.split())
    span = CONCISE_LIMIT_WORDS - CONCISE_TARGET_WORDS
    conciseness = 1.0 if words <= CONCISE_TARGET_WORDS else max(0.0, 1 - (words - CONCISE_TARGET_WORDS) / span)
    support = (cite_trace or {}).get("support") or []
    out = {"reply_words": float(words), "conciseness": round(conciseness, 3),
           "hallucinated_urls": float(len((cite_trace or {}).get("removed_urls") or []))}
    if support:
        out["unsupported_citation_rate"] = round(sum(1 for v in support if v["verdict"] != "ok") / len(support), 3)
    return out


async def answer_relevance(ticket_text: str, reply: str, request_id: str,
                           signoff: str = SIGN_OFF) -> Optional[float]:
    """Embedding similarity of the ticket (as a query) to the reply body (as
    a passage), with the same local model retrieval uses. None without rc_kg."""
    body = _reply_body(reply, signoff)
    if not body:
        return None
    scores = await kg_client.similarity(ticket_text, [body], request_id=request_id)
    return scores[0] if scores else None


def lower_is_better(key: str) -> bool:
    """Whether a smaller score is better for this feedback key."""
    return key in ("hallucination", "hallucinated_urls", "unsupported_citation_rate", "reply_words")


# ── LLM judge ───────────────────────────────────────────────────────────────
JUDGE_KEYS = ("hallucination", "correctness", "conciseness", "answer_relevance")

JUDGE_SYSTEM = """\
You are a strict evaluator of draft replies written by an assistant for a university \
Research Computing (HPC) help desk. You are given the researcher's ticket, the RC \
documentation sources the assistant was given, and the assistant's draft (its reply and \
its suggested steps), and sometimes a RUBRIC: what a reply to this kind of request must \
do. Judge only what is in front of you.

Return ONLY a JSON object with exactly these keys:
{"hallucination": {"score": <0..1>, "unsupported_claims": [<up to 5 short quotes>]},
 "correctness": {"score": <0..1>, "reason": "<one sentence>"},
 "conciseness": {"score": <0..1>, "reason": "<one sentence>"},
 "answer_relevance": {"score": <0..1>, "reason": "<one sentence>"},
 "rubric": [{"item": <the rubric line, copied>, "met": true | false | null, "reason": "<one sentence>"}]}

- hallucination: the share of the draft's factual claims (commands, limits, paths, \
policies, names) that are supported by NEITHER the sources NOR the ticket. 0 = every \
claim is supported; 1 = none is. General advice that needs no source is not a claim.
- correctness: would following this draft resolve the researcher's actual problem, \
given the sources? 1 = yes; 0.5 = partly or with a gap; 0 = no or wrong.
- conciseness: 1 = says what is needed without padding or repetition; 0 = very verbose.
- answer_relevance: 1 = addresses exactly what the ticket asks; 0 = off-topic.
- rubric: one entry per RUBRIC line, in order. met = true if the draft does it, false if \
it does not or does the opposite, null if the line does not apply to this ticket (for \
example a stage the ticket is not at). [] when there is no rubric.
"""


def _judge_user(ticket: str, context: str, draft: Dict[str, Any], rubric: Optional[List[str]] = None) -> str:
    """The judge's user message: ticket, sources, rubric, reply body and suggested steps."""
    steps = "\n".join("- " + s for s in draft.get("suggested_steps") or [])
    rub = "\n".join("- " + r for r in rubric or []) or "(none)"
    return ("--- TICKET ---\n%s\n\n--- SOURCES ---\n%s\n\n--- RUBRIC ---\n%s\n\n--- DRAFT REPLY ---\n%s\n\n"
            "--- SUGGESTED STEPS ---\n%s\n\nReturn the JSON object only." % (
                ticket.strip(), context.strip() or "(none)", rub,
                _reply_body(draft.get("draft_response") or ""), steps))


def _clip01(x: Any) -> Optional[float]:
    """`x` as a float clamped to [0, 1], or None if it is not a number."""
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return None


async def judge(ticket_text: str, kg_context: str, draft: Dict[str, Any],
                rubric: Optional[List[str]] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Grade one draft with the serving model. Never raises.

    Returns {"scores": {key: {"score", "comment"}}, "rubric": [{"item", "met",
    "reason"}], "verdict", "reasons", "latency_ms", "usage", "judge_model"}
    -- or {"error": ...} with the same timing fields when the call failed or
    nothing usable came back."""
    import time
    from .llm_client import _usage, extract_json, get_client
    settings = get_settings()
    t0 = time.perf_counter()
    client = get_client(settings)
    out: Dict[str, Any] = {"judge_model": settings.vllm_model_name}
    try:
        kwargs = dict(model=settings.vllm_model_name, temperature=0.0, max_tokens=900,
                      messages=[{"role": "system", "content": JUDGE_SYSTEM},
                                {"role": "user", "content": _judge_user(ticket_text, kg_context, draft, rubric)}])
        if timeout:
            kwargs["timeout"] = timeout
        try:
            completion = await client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
        except Exception:                          # noqa: BLE001 -- server without JSON mode
            completion = await client.chat.completions.create(**kwargs)
        out.update(usage=_usage(completion))
        verdict = extract_json(completion.choices[0].message.content or "")
    except Exception as exc:                       # noqa: BLE001 -- evaluation must never break drafting
        out.update(error=str(exc)[:300], latency_ms=int((time.perf_counter() - t0) * 1000))
        return out
    finally:
        await client.close()
    out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
    verdict = verdict if isinstance(verdict, dict) else {}
    scores: Dict[str, Dict[str, Any]] = {}
    for key in JUDGE_KEYS:
        item = verdict.get(key)
        score = _clip01(item.get("score")) if isinstance(item, dict) else None
        if score is None:
            continue
        comment = (item.get("reason") or "; ".join(str(c) for c in item.get("unsupported_claims") or []))[:500]
        scores[key] = {"score": score, "comment": comment}
    items = []
    given = verdict.get("rubric") if isinstance(verdict.get("rubric"), list) else []
    for i, line in enumerate(rubric or []):
        got = given[i] if i < len(given) and isinstance(given[i], dict) else {}
        met = got.get("met")
        items.append({"item": line, "met": met if met in (True, False) else None,
                      "reason": str(got.get("reason") or "")[:300]})
    if not scores:
        out["error"] = "the judge returned nothing usable"
        return out
    out.update(scores=scores, rubric=items)
    out["reasons"] = flags(out)
    out["verdict"] = "low" if out["reasons"] else "ok"
    return out


def flags(result: Dict[str, Any]) -> List[str]:
    """Why a judged draft should not go out as it is ([] = fine)."""
    settings = get_settings()
    s = result.get("scores") or {}
    out = []
    h = (s.get("hallucination") or {}).get("score")
    if h is not None and h > settings.judge_max_hallucination:
        claims = (s["hallucination"].get("comment") or "").strip()
        out.append("unsupported claims (%.0f%%)%s" % (h * 100, ": " + claims if claims else ""))
    c = (s.get("correctness") or {}).get("score")
    if c is not None and c < settings.judge_min_correctness:
        out.append("may not resolve the request: " + (s["correctness"].get("comment") or ""))
    out += ["not done: " + r["item"] + (" (%s)" % r["reason"] if r.get("reason") else "")
            for r in result.get("rubric") or [] if r.get("met") is False]
    return out


def better(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    """Whether judge result `b` is better than `a`: fewer flags, then a higher
    correctness-minus-hallucination."""
    def key(r):
        s = r.get("scores") or {}
        return (-len(r.get("reasons") or []),
                (s.get("correctness") or {}).get("score", 0) - (s.get("hallucination") or {}).get("score", 0))
    return key(b) > key(a)


def revision_note(result: Dict[str, Any]) -> str:
    """What the writer is told when its draft is rewritten for the judge."""
    return ("\n\nA REVIEWER CHECKED YOUR PREVIOUS DRAFT OF THIS REPLY AND FOUND THESE PROBLEMS. "
            "Write the draft again, fixing them, and change nothing else that was right:\n"
            + "\n".join("- " + r for r in result.get("reasons") or []))


def caveat(result: Dict[str, Any]) -> str:
    """The panel's "check before sending" line for a draft the judge flagged."""
    return "Quality check (LLM judge) -- review before sending: " + "; ".join(result.get("reasons") or [])


async def judge_and_record(request_id: str, ticket_text: str, kg_context: str, draft: Dict[str, Any],
                           rubric: Optional[List[str]] = None) -> None:
    """Background mode: grade the draft and append each grade as llm_judge
    feedback beside the run. Errors are recorded, never raised."""
    result = await judge(ticket_text, kg_context, draft, rubric)
    meta = {k: result.get(k) for k in ("judge_model", "latency_ms", "usage")}
    if result.get("error"):
        runlog.add_feedback(request_id, {"key": "llm_judge_error", "score": 0, "source": "llm_judge",
                                         "comment": result["error"], "metadata": meta})
        logger.warning("judge failed | id=%s %s", request_id, result["error"][:200])
        return
    for key, item in (result.get("scores") or {}).items():
        runlog.add_feedback(request_id, {"key": key, "score": item["score"], "source": "llm_judge",
                                         "comment": item["comment"], "lower_is_better": lower_is_better(key),
                                         "metadata": meta})
    for r in result.get("rubric") or []:
        if r.get("met") is not None:
            runlog.add_feedback(request_id, {"key": "rubric", "score": 1 if r["met"] else 0, "source": "llm_judge",
                                             "comment": "%s -- %s" % (r["item"], r.get("reason", "")),
                                             "metadata": meta})
    logger.info("judge ok | id=%s %s", request_id,
                " ".join("%s=%s" % (k, v["score"]) for k, v in (result.get("scores") or {}).items()))


def judge_mode() -> str:
    """"gate" | "background" | "off", from JUDGE_MODE and ONLINE_EVAL."""
    settings = get_settings()
    mode = (settings.judge_mode or "gate").strip().lower()
    if not settings.online_eval or mode not in ("gate", "background"):
        return "off"
    return mode


_tasks: set = set()


def schedule_judge(request_id: str, ticket_text: str, kg_context: str, draft: Dict[str, Any],
                   rubric: Optional[List[str]] = None) -> bool:
    """Background mode: start the LLM judge for this draft as a background
    task, if this draft is sampled. Returns whether it was started."""
    settings = get_settings()
    if judge_mode() != "background" or random.random() >= settings.online_eval_sample_rate:
        return False
    task = asyncio.create_task(judge_and_record(request_id, ticket_text, kg_context, draft, rubric))
    _tasks.add(task)                               # keep a reference until it finishes
    task.add_done_callback(_tasks.discard)
    return True
