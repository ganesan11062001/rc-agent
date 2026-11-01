"""Corrective retrieval (CORRECTIVE_RAG): when a result is weak, search again
in the documentation's words and keep the better result.

Corrective RAG (Yan et al., 2024) grades retrieved documents and, when they
are poor, rewrites the query and searches elsewhere -- in the paper, the web.
Here "elsewhere" is never the web (answers must be RC's, and ticket text
must not leave the university): it is the same graph, searched with the
query rewritten in the vocabulary RC's pages use. Researchers describe
symptoms ("my jobs have been waiting for days"); pages are written in the
system's words ("queue, priority, FairShare").

  weak     the result is low-confidence, or its best source's cross-encoder
           probability is below RERANK_WEAK_BELOW
  rewrite  the start of the query (the subject and the opening of the
           description) plus the docs' words for the symptoms it describes
           (SYMPTOMS) and the descriptions of the topics it names
           (topics.yaml) -- no model call, deterministic, in the trace
  compare  both results are scored by the cross-encoder against the ORIGINAL
           query, so the comparison is fair; the second is kept only if its
           best source scores CORRECTIVE_MIN_GAIN higher and clears the weak
           threshold
  else     the first result stands, still flagged low-confidence: the
           caller's "not backed by RC documentation" caveat applies

Needs the reranker: without a scorer that can compare the two results, the
step is skipped (and says so in the trace).

OFF by default (CORRECTIVE_RAG=false). Measured 2026-11-01
(eval/rerank_sweep.py, RERANK_MODE=score): with "keep if higher" it ran on
2-4 of 45 questions and lowered MRR 0.795 -> 0.780 -- "my interactive
session keeps getting cancelled" scored higher after the rewrite (0.22 ->
0.38) while the right page fell from 1st; "NSF data management plan" was
kept on noise (0.0388 -> 0.0395). The margin rule (CORRECTIVE_MIN_GAIN)
would have kept none of those. Turn it on when the reviewed-ticket cases
show tickets it fixes; re-run the sweep first.
"""

import logging
import re
from typing import Dict, List, Optional

from config import get_settings
from enrich.topics import load_topics, match_topics

from . import rerank
from .graphrag import RetrievalResult, known_gap, retrieve

logger = logging.getLogger(__name__)

# Symptom (as researchers write it) -> the words RC's pages use for it. The
# first six are rc-agent's query expansions (measured: "pending for days"
# put "Understanding the Queuing System" 13th; with these words 1st).
SYMPTOMS = [
    (re.compile(r"(?i)\b(pending|waiting|queued|not (yet )?start(ed|ing)|hasn'?t started|stuck in (the )?queue)\b"),
     "queue priority fairshare scheduling pending reason"),
    (re.compile(r"(?i)\b(killed|out of memory|oom|out_of_memory|oom-kill)\b"), "memory request --mem out of memory"),
    (re.compile(r"(?i)\bdisk quota exceeded\b|\bquota\b|\bno space left\b"), "storage quota usage"),
    (re.compile(r"(?i)\b(can'?t|cannot|unable to) (log ?in|ssh|connect)\b|\bconnection (refused|timed out)\b"),
     "ssh login connect"),
    (re.compile(r"(?i)\b(module|command) not found\b|\bno module named\b"), "module load software environment"),
    (re.compile(r"(?i)\b(idle|idlebot|gpu (utili[sz]ation|usage) (is )?low)\b"), "idlebot gpu utilization"),
    (re.compile(r"(?i)\b(time ?limit|timed? out|due to time|walltime)\b"), "time limit --time partition limits"),
    (re.compile(r"(?i)\b(jupyter|notebook|ood|open ?ondemand|desktop session)\b"), "Open OnDemand interactive apps"),
    (re.compile(r"(?i)\b(add|access|join)\b.{0,40}\b(group|project|lab|partition)\b"),
     "access request PI approval project group membership"),
    (re.compile(r"(?i)\b(transfer|copy|upload|download|globus|scp|rsync)\b"), "data transfer Globus transfer node"),
    (re.compile(r"(?i)\b(cuda|gpu not|nvidia-smi|no gpu)\b"), "GPU jobs CUDA gpu partition"),
    (re.compile(r"(?i)\b(conda|pip|install(ing|ed)?|package)\b"), "software installation conda environment"),
]
_HEAD_CHARS = 300


def is_weak(res: RetrievalResult, weak_below: float) -> Optional[str]:
    """Why `res` is weak ("low_confidence" | "rerank_top<x"), or None."""
    if res.low_confidence:
        return "low_confidence"
    top = res.trace.get("rerank_top")
    if top is not None and top < weak_below:
        return "rerank_top %.3f < %.2f" % (top, weak_below)
    return None


def rewrite(query: str) -> str:
    """The query in the documentation's words; "" when there is nothing to add."""
    added: List[str] = []
    for pattern, words in SYMPTOMS:
        if pattern.search(query):
            added.append(words)
    topics = load_topics()
    hits = match_topics(query, topics)
    for t in topics:
        if t.name in hits:
            added.append("%s: %s" % (t.name, t.description))
    if not added:
        return ""
    head = re.sub(r"\s+", " ", query).strip()[:_HEAD_CHARS]
    return "%s\n%s" % (head, "\n".join(dict.fromkeys(added)))


def retrieve_corrective(query: str, k: int = 6, include_events: Optional[bool] = None,
                        enabled: Optional[bool] = None) -> RetrievalResult:
    """retrieve(), then the corrective step when the result is weak. The
    result's trace gets `corrective`: whether it ran, why, the rewritten
    query, both top scores and which result was kept."""
    s = get_settings()
    on = s.corrective_rag if enabled is None else enabled
    first = retrieve(query, k=k, include_events=include_events)
    info: Dict = {"enabled": bool(on), "ran": False}
    first.trace["corrective"] = info
    if not on:
        return first
    reason = is_weak(first, s.rerank_weak_below)
    info["weak"] = reason
    if reason is None:
        return first
    if first.trace.get("rerank_top") is None or not rerank.available(s.rerank_model):
        info["skipped"] = "no reranker to compare results with (RERANK_MODE=%s)" % s.rerank_mode
        return first
    q2 = rewrite(query)
    if not q2:
        info["skipped"] = "nothing to rewrite: no known symptom or topic in the query"
        return first
    mode = s.rerank_mode if s.rerank_mode in ("replace", "fuse", "score") else "score"
    second = retrieve(q2, k=k, include_events=include_events, rerank_mode=mode, rerank_query=query)
    before, after = first.trace.get("rerank_top") or 0.0, second.trace.get("rerank_top") or 0.0
    keep = after >= before + s.corrective_min_gain and after >= s.rerank_weak_below
    info.update(ran=True, rewritten_query=q2, before_top=before, after_top=after,
                second_ms=second.trace["timings_ms"].get("total"),
                kept="rewrite" if keep else "first",
                sources_changed=sorted({x.page_url for x in second.sources} - {x.page_url for x in first.sources})
                if keep else [])
    if not keep:
        return first
    # The kept result answers the original query: report it under that query,
    # and judge its confidence by the cross-encoder (its vector scores are
    # against the rewrite, not comparable with LOW_CONFIDENCE_BELOW).
    second.query = query
    second.low_confidence = after < s.rerank_weak_below or known_gap(query) is not None
    second.trace["corrective"] = info
    second.trace["timings_ms"]["corrective_first_total"] = first.trace["timings_ms"].get("total")
    return second
