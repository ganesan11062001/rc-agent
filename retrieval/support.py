"""Does the source a claim cites actually support it?

`citations.render` guarantees every link came from retrieval. This module
checks the next thing: that "[S2]" is attached to a sentence section S2
actually says. The model chose S2 out of the k sources it was shown, so the
question asked is relative -- is there another of those k that fits the claim
clearly better? -- rather than "is the similarity above some number".

Why relative, measured on this corpus (2026-10-22): an absolute cosine
threshold separates supporting from non-supporting sections poorly (AUC 0.76
for ticket-style claims; any threshold that keeps 90% of correct citations
misses most wrong ones), because similarity levels differ a lot between
pages. Ranking the cited source against the other retrieved ones, with a
small bonus for shared distinctive terms (`seff`, `/scratch`, partition
names -- what embeddings blur), puts the true source first for 92% of claims;
flagging only when another source wins by >= 0.06 false-flags 1.6% of correct
citations and catches 96% of citations to the wrong source. Re-checked on
held-out claims through this module: 2.6% false flags, 94-95% caught. Those
claims were derived from section text; real model paraphrases will be
somewhat harder -- validate on real drafts (eval/citation_review.py).

The verdicts are advisory. A flagged citation is reported, never rewritten:
a human reviews every draft, and a confident silent swap would be worse than
a caveat.
"""

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import get_settings
from graph.connection import driver

from .embeddings import get_embedder
from .graphrag import _STOPWORDS

LEXICAL_WEIGHT = 0.3
MARGIN = 0.06
# Below this combined score the cited section shares nothing meaningful with
# the claim, even if no other source does better (e.g. a citation tacked onto
# a greeting). 99% of correct citations score >= 0.68 on the held-out set.
FLOOR = 0.65

_MARKER = re.compile(r"\[(S\d+(?:\s*,\s*S\d+)*)\]")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-/]*[A-Za-z0-9]")
# A claim runs back from its marker to the previous sentence end or marker.
_BOUNDARY = re.compile(r"(?:[.!?](?=\s)|\n|\[S\d+(?:\s*,\s*S\d+)*\])")


@dataclass
class Verdict:
    ref: str
    claim: str
    verdict: str                      # "ok" | "better_source" | "weak"
    score: float
    best_ref: Optional[str] = None    # set when verdict == "better_source"
    best_score: Optional[float] = None


def extract_claims(text: str) -> List[Tuple[str, List[str]]]:
    """[(claim text, [refs cited for it])] in order of appearance."""
    out = []
    for m in _MARKER.finditer(text):
        before = text[: m.start()]
        cut = 0
        for b in _BOUNDARY.finditer(before):
            # A sentence end directly before the marker ("...memory. [S1]")
            # belongs to this claim, not the boundary before it.
            if b.end() < len(before.rstrip()):
                cut = b.end()
        claim = before[cut:].strip(" \t-*:;,")
        refs = re.split(r"\s*,\s*", m.group(1))
        if claim:
            out.append((claim, refs))
    return out


def _page(url: str) -> str:
    return url.split("#", 1)[0].rstrip("/")


def _tokens(text: str) -> set:
    return {t.lower() for t in _TOKEN.findall(text)
            if len(t) > 2 and t.lower() not in _STOPWORDS}


def _sections(urls: List[str]) -> Dict[str, Dict]:
    s = get_settings()
    with driver().session(database=s.neo4j_database) as sess:
        rows = sess.run(
            "MATCH (s:Section) WHERE s.url_with_anchor IN $urls "
            "RETURN s.url_with_anchor AS url, s.heading AS heading, s.text AS text, "
            "s.embedding AS vec", urls=urls)
        return {r["url"]: dict(r) for r in rows}


def check(texts: List[str], refs: Dict[str, str]) -> List[Verdict]:
    """Judge every [S#] citation in `texts` against the sources in `refs`
    (S# -> url_with_anchor, i.e. everything that was retrieved)."""
    claims = [c for t in texts for c in extract_claims(t or "")]
    if not claims:
        return []
    secs = _sections(list(refs.values()))
    cand = {ref: secs[url] for ref, url in refs.items() if url in secs and secs[url]["vec"]}
    if not cand:
        return []
    for sec in cand.values():
        sec["tokens"] = _tokens("%s %s" % (sec["heading"], sec["text"]))
        sec["vec"] = np.asarray(sec["vec"], dtype=np.float32)

    emb = get_embedder()
    verdicts: List[Verdict] = []
    for claim, cited in claims:
        cvec = np.asarray(emb.embed_query(claim), dtype=np.float32)
        ctok = _tokens(claim)

        def score(ref: str) -> Tuple[float, float]:
            sec = cand[ref]
            cos = float(cvec @ sec["vec"])
            lex = len(ctok & sec["tokens"]) / max(1, len(ctok))
            return cos + LEXICAL_WEIGHT * lex, cos

        scored = {ref: score(ref) for ref in cand}
        for ref in cited:
            if ref not in scored:
                continue                      # unknown refs are render()'s job
            total, cos = scored[ref]
            # A better section on the same page is not a wrong citation: the
            # reader lands on the right page either way.
            page = _page(refs[ref])
            rivals = [r for r in scored if r not in cited and _page(refs[r]) != page]
            best_ref = max(rivals, key=lambda r: scored[r][0]) if rivals else None
            if best_ref and scored[best_ref][0] - total >= MARGIN:
                verdicts.append(Verdict(ref, claim, "better_source", round(total, 3),
                                        best_ref, round(scored[best_ref][0], 3)))
            elif total < FLOOR:
                verdicts.append(Verdict(ref, claim, "weak", round(total, 3)))
            else:
                verdicts.append(Verdict(ref, claim, "ok", round(total, 3)))
    return verdicts
