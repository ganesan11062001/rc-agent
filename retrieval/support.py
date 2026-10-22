"""Does the source a claim cites actually support it?

`citations.render` guarantees every link came from retrieval. This module
checks the next thing: that "[S2]" is attached to a sentence section S2
actually says. The model chose S2 out of the k sources it was shown, so the
question asked is relative -- is there another of those k that fits the claim
clearly better? -- rather than "is the similarity above some number".

Why relative, measured on this corpus (2026-10-22): an absolute cosine
threshold separates supporting from non-supporting sections poorly (AUC 0.76
for ticket-style claims), because similarity levels differ a lot between
pages. So a citation is flagged only when a section on another retrieved page
beats the cited page's best retrieved section by >= 0.06 on cosine + 0.3 x
shared distinctive terms (`seff`, `/scratch`, partition names -- what
embeddings blur) AND on cosine alone. Terms alone are not enough: the OOM page
mentions seff and the seff page mentions memory requests, and term overlap
alone flagged correct citations of both.

Measured by eval/citation_check.py (re-run whenever retrieval changes, since
it changes which sources the check chooses between):
  hand-labelled draft sentences  1/11 correct citations flagged, 11/11 wrong caught
  held-out reworded sentences    3.4% flagged, 87% wrong caught (178 each)
Known weaknesses:
- A short section that says the thing tersely ("#SBATCH --mem=3G") can lose
  to a longer page that discusses the topic; that is the one hand-labelled
  false flag.
- It can only point to a better source among those retrieved. A claim no
  retrieved page supports, cited to a loosely related one, is caught only by
  FLOOR -- seen in testing: a GPU-partition limit cited to the OOM section
  of an OOM ticket's sources was not flagged.

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
# Below this the cited page shares nothing meaningful with the claim, even if
# no other page does better (e.g. a citation tacked onto a greeting). Swept
# 0.50-0.65 on both eval sets: it changes 1 catch in 130 and no false flags
# there, but 0.65 flagged a correctly cited, tersely worded draft sentence
# (score 0.64), so it is set low.
FLOOR = 0.55

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
            # The reader lands on the cited page, so the page is credited with
            # its best retrieved section, and same-page sections are not rivals.
            page = _page(refs[ref])
            mine = [r for r in scored if _page(refs[r]) == page]
            total = max(scored[r][0] for r in mine)
            cos = max(scored[r][1] for r in mine)
            rivals = [r for r in scored if r not in cited and _page(refs[r]) != page]
            best_ref = max(rivals, key=lambda r: scored[r][0]) if rivals else None
            # Both signals must agree. Shared terms alone are not enough: on
            # this site the OOM page mentions seff and the seff page mentions
            # memory requests, so term overlap prefers the other page for
            # claims both pages support.
            if best_ref and scored[best_ref][0] - total >= MARGIN \
                    and scored[best_ref][1] > cos:
                verdicts.append(Verdict(ref, claim, "better_source", round(total, 3),
                                        best_ref, round(scored[best_ref][0], 3)))
            elif total < FLOOR:
                verdicts.append(Verdict(ref, claim, "weak", round(total, 3)))
            else:
                verdicts.append(Verdict(ref, claim, "ok", round(total, 3)))
    return verdicts
