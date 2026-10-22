"""Grounding from the RC knowledge graph (rc_kg), when one is configured.

rc_kg runs in the same allocation (see ood-apps/rc-copilot/template/script.sh.erb)
and serves the rc.northeastern.edu website as a Neo4j GraphRAG index. For each
ticket we ask it for the most relevant website sections, put them in the
prompt as numbered sources [S1]..[Sn], and afterwards turn the model's [S#]
markers into exact links. The model never writes a URL itself: any URL it does
write is checked against what was retrieved and dropped if it is not there.

Everything here degrades to "no grounding": if RC_KG_URL is unset, or rc_kg is
still loading or down, drafting works exactly as it did before rc_kg existed,
plus a caveat saying the draft was not checked against the website.

Plain urllib on a worker thread rather than an HTTP client library: it needs
no new dependency, and an empty ProxyHandler keeps the site proxy (which 403s
loopback) out of a call that never leaves the node.
"""

import asyncio
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import get_settings

logger = logging.getLogger(__name__)

_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# The rc_kg API major version this client is written against; see
# rc_kg/API_CONTRACT.md. A different major means fields this file reads may
# have changed, so grounding is switched off rather than guessed at.
EXPECTED_API_MAJOR = "1"
_compatible: Optional[bool] = None       # cached once rc_kg has answered

# The reply is sent as written, so its sign-off is fixed by the prompt.
# References go above it rather than after "Research Computing".
_SIGN_OFF = "Best regards,\nResearch Computing"

_MARKERS = re.compile(r"\s*\[S\d+(?:\s*,\s*S\d+)*\]")


def _strip_markers(text: str) -> str:
    return _MARKERS.sub("", text)


def strip_citations(draft: Dict[str, Any]) -> Dict[str, Any]:
    """Remove [S#] markers from every field. For drafts made without sources:
    a model can still emit them, and a raw "[S3]" must not reach a researcher."""
    draft.update(
        draft_response=_strip_markers(draft["draft_response"]),
        problem_summary=_strip_markers(draft["problem_summary"]),
        suggested_steps=[_strip_markers(x) for x in draft["suggested_steps"]],
        caveats=[_strip_markers(x) for x in draft["caveats"]],
    )
    return draft


@dataclass
class KGContext:
    status: str                      # "ok" | "unavailable" | "disabled"
    context: str = ""
    low_confidence: bool = False
    sources: List[Dict[str, Any]] = field(default_factory=list)
    allowed_urls: List[str] = field(default_factory=list)
    detail: Optional[str] = None


def _call(path: str, body: Optional[Dict] = None, timeout: float = 10.0) -> Dict:
    s = get_settings()
    req = urllib.request.Request(
        s.rc_kg_url.rstrip("/") + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer %s" % s.rc_kg_api_token}
                    if s.rc_kg_api_token else {})},
        method="POST" if body is not None else "GET",
    )
    with _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


async def _acall(path: str, body: Optional[Dict] = None,
                 timeout: Optional[float] = None) -> Dict:
    t = timeout if timeout is not None else get_settings().rc_kg_timeout_seconds
    return await asyncio.to_thread(_call, path, body, t)


def _why(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            return "HTTP %d: %s" % (exc.code, json.loads(exc.read()).get("detail"))
        except Exception:                          # noqa: BLE001
            return "HTTP %d" % exc.code
    return str(getattr(exc, "reason", exc))


def _version_problem(h: Dict[str, Any]) -> Optional[str]:
    got = str(h.get("api_version") or "")
    if got.split(".")[0] != EXPECTED_API_MAJOR:
        return ("rc_kg API version %s, but this TicketMate needs %s.x"
                % (got or "unknown", EXPECTED_API_MAJOR))
    return None


async def _check_compatible() -> Optional[str]:
    """None if rc_kg speaks our major version, else why not. Checked once
    per process: the rc_kg serving a job does not change under it."""
    global _compatible
    if _compatible:
        return None
    h = await _acall("/health", timeout=3.0)
    problem = _version_problem(h)
    if problem is None and h.get("status") == "ok":
        _compatible = True
    return problem


async def retrieve(ticket_text: str) -> KGContext:
    s = get_settings()
    if not s.rc_kg_url:
        return KGContext(status="disabled")
    try:
        problem = await _check_compatible()
    except Exception as exc:                       # noqa: BLE001
        return KGContext(status="unavailable", detail=_why(exc))
    if problem:
        logger.error("rc_kg incompatible: %s", problem)
        return KGContext(status="unavailable", detail=problem)
    try:
        res = await _acall("/retrieve", {"query": ticket_text, "k": s.rc_kg_k})
    except Exception as exc:                       # noqa: BLE001
        logger.warning("rc_kg retrieve failed: %s", _why(exc))
        return KGContext(status="unavailable", detail=_why(exc))
    logger.info("rc_kg | sources=%d topics=%s low_confidence=%s",
                len(res["sources"]), ",".join(res["query_topics"]) or "-",
                res["low_confidence"])
    return KGContext(status="ok", context=res["context"],
                     low_confidence=res["low_confidence"],
                     sources=res["sources"], allowed_urls=res["allowed_urls"])


async def health() -> Tuple[str, Optional[str]]:
    """("ok"|"loading"|"unreachable"|"incompatible"|"disabled", detail)."""
    s = get_settings()
    if not s.rc_kg_url:
        return "disabled", None
    try:
        h = await _acall("/health", timeout=3.0)
    except Exception as exc:                       # noqa: BLE001
        return "unreachable", _why(exc)
    problem = _version_problem(h)
    if problem:
        return "incompatible", problem
    if h.get("status") == "ok":
        built = (h.get("snapshot") or {}).get("built_at")
        return "ok", ("graph built %s" % built) if built else None
    return h.get("status", "unknown"), h.get("detail")


def _title(src: Dict[str, Any]) -> str:
    # Some headings end in ":" ("Solution:"), and the title is followed by one.
    heading = src["heading"].rstrip(": ")
    if heading in ("Overview", src["page_title"]):
        return src["page_title"]
    return "%s - %s" % (src["page_title"], heading)


async def apply_citations(draft: Dict[str, Any], kg: KGContext) -> Tuple[Dict[str, Any], List[Dict]]:
    """Turn [S#] markers into [1], [2].. with one numbering across the whole
    draft, add a References block to the reply and a Sources line to the
    steps, and drop any URL the model wrote that retrieval did not return.

    Returns (draft, references). On any rc_kg error the draft is returned with
    markers stripped, so a raw "[S3]" never reaches a researcher.
    """
    steps = list(draft["suggested_steps"])
    caveats = list(draft["caveats"])
    # Reply first, so the researcher's References read 1, 2, 3 in order.
    texts = [draft["draft_response"], draft["problem_summary"], *steps, *caveats]
    refs = {src["ref"]: src["url"] for src in kg.sources}
    try:
        out = await _acall("/cite", {"texts": texts, "refs": refs,
                                     "allowed_urls": kg.allowed_urls,
                                     "style": "numbered", "check_support": True})
    except Exception as exc:                       # noqa: BLE001
        logger.warning("rc_kg cite failed, stripping markers: %s", _why(exc))
        return strip_citations(draft), []

    t = out["texts"]
    reply, summary = t[0], t[1]
    new_steps = t[2:2 + len(steps)]
    new_caveats = t[2 + len(steps):]

    by_ref = {src["ref"]: src for src in kg.sources}
    references = [{"n": r["n"], "title": _title(by_ref[r["ref"]]), "url": r["url"]}
                  for r in out["references"]]
    if references:
        block = "References:\n" + "\n".join(
            "[%d] %s: %s" % (r["n"], r["title"], r["url"]) for r in references)
        if _SIGN_OFF in reply:
            head, tail = reply.rsplit(_SIGN_OFF, 1)
            reply = "%s\n\n%s\n\n%s%s" % (head.rstrip(), block, _SIGN_OFF, tail)
        else:
            reply = "%s\n\n%s" % (reply.rstrip(), block)
        # Steps go to work notes, which may not include the reply, so they
        # carry their own copy of the links.
        new_steps.append("Sources: " + "; ".join(
            "[%d] %s" % (r["n"], r["url"]) for r in references))
    # Citations whose page does not seem to say what the sentence claims.
    # Advisory: the link is left as the model chose it, for the engineer to judge.
    number = {r["ref"]: r["n"] for r in out["references"]}
    for v in out.get("support") or []:
        if v["verdict"] == "ok" or v["ref"] not in number:
            continue
        claim = v["claim"] if len(v["claim"]) <= 120 else v["claim"][:117] + "..."
        msg = 'Check citation [%d] on "%s": ' % (number[v["ref"]], claim)
        if v["verdict"] == "better_source" and v["best_ref"] in by_ref:
            msg += 'the page "%s" (%s) matches it better.' % (
                _title(by_ref[v["best_ref"]]), by_ref[v["best_ref"]]["url"])
        else:
            msg += "the cited page does not clearly say this."
        new_caveats.append(msg)
    if out["removed_urls"]:
        new_caveats.append(
            "Removed %d link(s) the model wrote that were not among the RC pages "
            "it was given: %s" % (len(out["removed_urls"]), ", ".join(out["removed_urls"])))

    draft.update(draft_response=reply, problem_summary=summary,
                 suggested_steps=new_steps, caveats=new_caveats)
    return draft, references
