"""Hybrid retrieval over the graph, packaged as LLM context.

1. Seeds: vector search and fulltext search over retrievable sections, fused
   with reciprocal rank fusion. A third, weaker signal comes from topics: the
   ticket's words are matched against topics.yaml and sections that MENTION
   those topics get a small boost. Vector finds paraphrases ("my job got
   killed" -> the OOM section); fulltext finds exact tokens the embedding
   blurs (`seff`, `gpu-interactive`, an error string).
2. Expansion, per seed, through the graph: the neighbouring sections on the
   same page (NEXT, both ways), the site pages it links to (LINKS_TO), the
   external docs it links to (LINKS_OUT -- mostly rc-docs, where the commands
   live), and its topics.
3. Output: numbered sources [S1].. with exact URLs, a context block to paste
   into a prompt, and the set of URLs an answer is allowed to contain.

The LLM is told to cite [S1]-style markers, never to write URLs. The markers
are turned into links by `citations.render`, so every link in an answer comes
from the graph.
"""

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Dict, List, Optional

from config import get_settings
from enrich.topics import load_topics, match_topics
from graph.connection import driver

from .embeddings import get_embedder

logger = logging.getLogger(__name__)

# RRF constant. 60 is the value from the original paper and is not sensitive.
_RRF_K = 60
_TOPIC_WEIGHT = 0.3     # topic boost is a tie-breaker, not a retriever
# Topic hits only re-rank sections vector or fulltext already found. Letting
# them introduce sections (the first version did) is query-independent noise:
# every "Storage" ticket pulled in the same long storage-heavy sections, e.g.
# the 11,682-char cluster status page, and with the docs site added that
# pushed correct answers out of the top k.
_TOPIC_INTRODUCES = False
_FULLTEXT_WEIGHT = 1.0
# "flat": every matched topic counts the same. "idf": a topic counts by how
# specific it is -- "Explorer cluster" is matched by nearly every ticket and
# mentioned by hundreds of sections, so it should not move the ranking;
# "Globus" should. "off": topics do not affect ranking at all.
# Measured on the 45-question eval with the docs site (2026-10-22): flat kept
# "connect from Windows" out of the top 5 (Hit@5 0.956); idf and off both fix
# it (0.978). idf is used because it keeps specific topics useful. RRF k and
# the fulltext weight moved MRR by <0.02 with no consistent direction, i.e.
# noise at this eval size, so they stay at their defaults.
_TOPIC_MODE = "idf"
# At most this many sources from one page, so one long page cannot fill k.
_MAX_PER_PAGE = 3

# Status posts (outages, maintenance, purges) are news: last April's "Explorer
# and Discovery are Down" is not why a node is down today, yet it ranked in
# the top 6 for node-down tickets. Once a status post is older than this, its
# score is multiplied by _STALE_FACTOR -- unless the ticket is itself about an
# outage or maintenance, where an old post may be the one being asked about.
# Down-weighted rather than dropped: for "globus" on the website alone, the
# Globus maintenance post is still the best page there is. Swept 2026-10-26:
# the node-down regression cases pass at 0.4-0.7 alike (the outage alias and
# the short query fix those); 0.4 pushed "globus" out of the website top 5,
# 0.6 keeps it and gives the best MRR on both graphs.
_STATUS_CATEGORIES = {"Status Updates", "Explorer Status", "Discovery Status", "AICR"}
_OUTAGE_TOPIC = "Maintenance and outages"
_STALE_AFTER_DAYS = 30
_STALE_FACTOR = 0.6

# Best vector score under which the result is flagged low_confidence; see
# where it is used for the measurement.
LOW_CONFIDENCE_BELOW = 0.83

# Problems no page covers, recognised by what the ticket says rather than by
# score: one node down or drained, the cluster being slow. On 8 such real
# tickets (2026-10-26/27) the best vector score was 0.833-0.882 -- inside the
# range of covered tickets and of the eval questions -- so no threshold, nor
# vector/fulltext agreement, separates them. These patterns flag 6/6 of the
# node and slowdown tickets and none of the 45 eval questions.
_NODE_NAME = re.compile(r"(?<![\w./-])[a-z]{1,2}\d{4}(?![\w-])")
_NODE_FAULT = re.compile(r"(?i)\b(down|drain(ed|ing)?|not responding|unresponsive|ReqNodeNotAvail|unavailable|"
                         r"instability|unstable|kill task failed|keeps? going down)\b")
_SLOWNESS = re.compile(r"(?i)\b(slow(ness|down|ly)?|sluggish|laggy|hangs? for minutes)\b")
_CLUSTER_WORDS = re.compile(r"(?i)\b(cluster|explorer|login|directories|files?|navigat\w+|ood|open ondemand)\b")


def known_gap(query: str) -> Optional[str]:
    """Why no page will cover this query, or None."""
    if _NODE_NAME.search(query) and _NODE_FAULT.search(query):
        return "a node fault"
    if _SLOWNESS.search(query) and _CLUSTER_WORDS.search(query):
        return "cluster slowness"
    return None

# Tickets are long and full of log noise. Fulltext gets the distinctive words,
# capped so the Lucene query stays small.
_MAX_FULLTEXT_TERMS = 40
_STOPWORDS = set("""
a about above after again all also am an and any are as at be because been
before being below between both but by can cannot could did do does doing
don down during each few for from further get got had has have having he her
here hers him his how i if in into is it its itself just me more most my no
nor not now of off on once only or other our out over own please same she
should so some such than thank thanks that the their them then there these
they this those through to too under until up very was we were what when
where which while who whom why will with would you your yours hi hello dear
regards best team research computing ticket issue problem help need trying
able unable using use used work working works
""".split())

_LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')


@dataclass
class Source:
    """One numbered source in a retrieval result: a section, the URL to cite,
    its page metadata, fused score and per-retriever ranks, and the
    neighbouring text, topics and links the graph expansion added."""

    ref: str                       # "S1"
    section_id: str
    page_title: str
    heading: str
    url: str                       # url_with_anchor -- what gets cited
    page_url: str
    content_type: str
    site: str                      # "rc.northeastern.edu" | "rc-docs.northeastern.edu"
    published: str
    last_modified: str
    anchor_kind: str
    text: str
    score: float
    vector_score: Optional[float] = None
    signals: Dict[str, int] = field(default_factory=dict)   # retriever -> rank
    context_before: str = ""
    context_after: str = ""
    topics: List[str] = field(default_factory=list)
    links: List[Dict[str, str]] = field(default_factory=list)      # site pages
    external_links: List[str] = field(default_factory=list)


@dataclass
class RetrievalResult:
    """What retrieve() returns and POST /retrieve serialises: the sources, the
    prompt context built from them, the URLs an answer may contain, and
    whether the best match was too weak to trust."""

    query: str
    query_topics: List[str]
    sources: List[Source]
    context: str
    allowed_urls: List[str]
    low_confidence: bool
    # How this result was produced, for traceability: per-stage timings,
    # candidate counts, the ranking settings and the best vector score. No
    # query text (the caller has it).
    trace: Dict = field(default_factory=dict)


def _fulltext_query(text: str) -> str:
    """Turn free text into a Lucene OR-query of its distinctive terms
    (stopwords, short and numeric tokens dropped, special characters
    escaped, capped at _MAX_FULLTEXT_TERMS); "" if nothing is left."""
    seen, terms = set(), []
    for tok in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.\-/]*[A-Za-z0-9]|[A-Za-z0-9]", text):
        low = tok.lower()
        if len(low) < 3 or low in _STOPWORDS or low.isdigit() or low in seen:
            continue
        seen.add(low)
        esc = _LUCENE_SPECIAL.sub(r"\\\1", tok)
        # The index tokenizes without stemming, and the site writes compounds
        # as one word, so "Jupyter" never matched "JupyterLab", nor "partition"
        # "partitions". A prefix form catches both; the exact form stays so an
        # exact hit still scores higher.
        if len(low) >= 4 and tok.isalnum():
            terms.append("%s OR %s*" % (esc, esc))
        else:
            terms.append(esc)
        if len(terms) >= _MAX_FULLTEXT_TERMS:
            break
    return " OR ".join(terms)


VECTOR = """
CALL db.index.vector.queryNodes('section_embeddings', $n, $vec)
YIELD node, score
WHERE node.retrievable OR ($events AND node.duplicate_of IS NULL
      AND exists { (:Page {content_type: 'calendar_event'})-[:HAS_SECTION]->(node) })
RETURN node.section_id AS sid, score
"""

FULLTEXT = """
CALL db.index.fulltext.queryNodes('section_search', $q, {limit: $n})
YIELD node, score
WHERE node.retrievable OR ($events AND node.duplicate_of IS NULL
      AND exists { (:Page {content_type: 'calendar_event'})-[:HAS_SECTION]->(node) })
RETURN node.section_id AS sid, score
"""

TOPIC_SECTIONS = """
MATCH (s:Section)-[m:MENTIONS]->(t:Topic)
WHERE t.name IN $topics AND s.retrievable
WITH s, sum(CASE WHEN m.in_heading THEN 3 ELSE 1 END * m.hits) AS weight
RETURN s.section_id AS sid, weight
ORDER BY weight DESC LIMIT $n
"""

# Per topic: idf = log(N / df) / log(N), in [0, 1]; 0 for a topic every
# retrievable section mentions, 1 for one only a single section mentions.
TOPIC_IDF = """
MATCH (x:Section {retrievable: true}) WITH count(x) AS n
UNWIND $topics AS name
MATCH (t:Topic {name: name})
OPTIONAL MATCH (s:Section {retrievable: true})-[:MENTIONS]->(t)
WITH n, name, count(s) AS df
RETURN name, CASE WHEN df = 0 THEN 0.0 ELSE log(toFloat(n) / df) / log(toFloat(n)) END AS idf
"""

EXPAND = """
UNWIND $sids AS sid
MATCH (p:Page)-[:HAS_SECTION]->(s:Section {section_id: sid})
OPTIONAL MATCH (prev:Section)-[:NEXT]->(s)
OPTIONAL MATCH (s)-[:NEXT]->(nxt:Section)
RETURN sid,
       p.published AS published, p.last_modified AS last_modified,
       coalesce(p.site, 'rc.northeastern.edu') AS site,
       p.title AS page_title, p.url AS page_url, p.content_type AS content_type,
       s.heading AS heading, s.text AS text, s.url_with_anchor AS url,
       s.anchor_kind AS anchor_kind,
       prev.heading AS prev_heading, prev.text AS prev_text,
       nxt.heading AS next_heading, nxt.text AS next_text,
       [(s)-[:MENTIONS]->(t:Topic) | t.name] AS topics,
       [(p)-[:IN_CATEGORY]->(c:Category) | c.name] AS categories,
       [(s)-[:LINKS_TO]->(q:Page) | {title: q.title, url: q.url + '/'}] AS links,
       [(s)-[:LINKS_OUT]->(e:ExternalLink) | e.url] AS external_links
"""


def _is_stale_status_post(row: Dict, today: date) -> bool:
    """True for a status/outage post (by category or topic) published more
    than _STALE_AFTER_DAYS before `today`. `row` is an EXPAND result row."""
    if row["content_type"] != "post" or not (
            set(row["categories"] or []) & _STATUS_CATEGORIES or _OUTAGE_TOPIC in (row["topics"] or [])):
        return False
    try:
        published = datetime.fromisoformat((row["published"] or "")[:10]).date()
    except ValueError:
        return False
    return (today - published).days > _STALE_AFTER_DAYS


def _clip(text: Optional[str], n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n].rsplit(" ", 1)[0] + " …"


def retrieve(query: str, k: int = 6, include_events: Optional[bool] = None,
             candidates: int = 30, today: Optional[date] = None) -> RetrievalResult:
    """Retrieve the top `k` sources for `query` (see the module docstring).

    `include_events` None uses INCLUDE_EVENTS_IN_RETRIEVAL. `candidates` is
    how many hits each retriever contributes before fusion. `today` is for
    tests: it fixes the date stale status posts are measured from. Raises
    ValueError on an empty query and RuntimeError if the graph has no
    embeddings or was embedded with a different model.
    """
    s = get_settings()
    events = s.include_events_in_retrieval if include_events is None else include_events
    query = query.strip()
    if not query:
        raise ValueError("empty query")

    t_start = time.perf_counter()
    timings: Dict[str, float] = {}
    counts: Dict[str, int] = {}

    def lap(name: str, since: float) -> float:
        """Record ms since `since` as timing `name`; returns now for the next lap."""
        now = time.perf_counter()
        timings[name] = round((now - since) * 1000, 1)
        return now

    emb = get_embedder()
    topics = load_topics()
    query_topics = sorted(match_topics(query, topics))

    fused: Dict[str, float] = {}
    signals: Dict[str, Dict[str, int]] = {}
    vector_scores: Dict[str, float] = {}

    def add(sid: str, retriever: str, rank: int, weight: float = 1.0) -> None:
        """Add one retriever's weighted RRF contribution for `sid` at `rank`."""
        fused[sid] = fused.get(sid, 0.0) + weight / (_RRF_K + rank)
        signals.setdefault(sid, {})[retriever] = rank

    with driver().session(database=s.neo4j_database) as sess:
        model_rows = sess.run(
            "MATCH (s:Section) WHERE s.embedding_model IS NOT NULL "
            "RETURN DISTINCT s.embedding_model AS m"
        ).value("m")
        if not model_rows:
            raise RuntimeError("no embeddings in the graph -- run `python -m enrich.run`")
        if model_rows != [emb.model]:
            raise RuntimeError(
                "graph was embedded with %s but EMBEDDING_MODEL=%s; re-run "
                "`python -m enrich.run --reembed`" % (model_rows, emb.model)
            )

        t = time.perf_counter()
        vec = emb.embed_query(query)
        t = lap("embed", t)
        for rank, r in enumerate(sess.run(VECTOR, n=candidates, vec=vec, events=events), 1):
            add(r["sid"], "vector", rank)
            vector_scores[r["sid"]] = r["score"]
        counts["vector"] = len(vector_scores)
        t = lap("vector", t)

        ft = _fulltext_query(query)
        n_ft = 0
        if ft:
            for rank, r in enumerate(
                sess.run(FULLTEXT, q=ft, n=candidates, events=events), 1
            ):
                add(r["sid"], "fulltext", rank, _FULLTEXT_WEIGHT)
                n_ft = rank
        counts["fulltext"] = n_ft
        t = lap("fulltext", t)
        topic_weight = 0.0

        if query_topics and _TOPIC_MODE != "off":
            topic_weight = _TOPIC_WEIGHT
            if _TOPIC_MODE == "idf":
                idf = {r["name"]: r["idf"] for r in sess.run(TOPIC_IDF, topics=query_topics)}
                # The ticket's most specific topic sets how much topics count.
                topic_weight = _TOPIC_WEIGHT * max(idf.values(), default=0.0)
            found = set(fused)
            for rank, r in enumerate(
                sess.run(TOPIC_SECTIONS, topics=query_topics, n=candidates), 1
            ):
                if _TOPIC_INTRODUCES or r["sid"] in found:
                    add(r["sid"], "topic", rank, topic_weight)
                    counts["topic"] = counts.get("topic", 0) + 1
        t = lap("topics", t)

        order = sorted(fused, key=fused.get, reverse=True)
        counts["fused"] = len(order)
        rows = {r["sid"]: r for r in sess.run(EXPAND, sids=order[: k * 4])}
        lap("expand", t)

    today = today or date.today()
    outage_ticket = _OUTAGE_TOPIC in query_topics
    stale = 0
    for sid in order[: k * 4]:
        if not outage_ticket and _is_stale_status_post(rows[sid], today):
            fused[sid] *= _STALE_FACTOR
            signals[sid]["stale"] = 1
            stale += 1
    counts["stale_status_posts"] = stale
    order = sorted(order[: k * 4], key=fused.get, reverse=True)

    ranked: List[str] = []
    per_page: Dict[str, int] = {}
    for sid in order:
        page = rows[sid]["page_url"]
        if per_page.get(page, 0) >= _MAX_PER_PAGE:
            continue
        per_page[page] = per_page.get(page, 0) + 1
        ranked.append(sid)
        if len(ranked) == k:
            break

    sources: List[Source] = []
    for i, sid in enumerate(ranked, 1):
        r = rows[sid]
        sources.append(Source(
            ref="S%d" % i,
            section_id=sid,
            page_title=r["page_title"],
            heading=r["heading"],
            url=r["url"],
            page_url=r["page_url"] + "/",
            content_type=r["content_type"],
            site=r["site"],
            published=(r["published"] or "")[:10],
            last_modified=(r["last_modified"] or "")[:10],
            anchor_kind=r["anchor_kind"],
            text=r["text"],
            score=round(fused[sid], 5),
            vector_score=round(vector_scores[sid], 4) if sid in vector_scores else None,
            signals=signals[sid],
            context_before=_clip(r["prev_text"], 400) if r["prev_text"] else "",
            context_after=_clip(r["next_text"], 400) if r["next_text"] else "",
            topics=sorted(r["topics"]),
            links=sorted({l["url"]: l for l in r["links"]}.values(), key=lambda l: l["url"]),
            external_links=sorted(set(r["external_links"])),
        ))

    # Below the gap, tell the caller rather than let the LLM answer
    # confidently from noise. 0.78 was set on whole-ticket queries, which
    # score high whatever they say: on 19 real tickets it never fired, not even
    # for a node outage no page covers. Re-measured 2026-10-26 on short
    # queries (subject, description, latest message), bge-small, full graph:
    # the 45 eval questions score 0.840-0.93, twelve off-topic questions
    # (parking, Canvas, Outlook, payroll) at most 0.801, and the node-down
    # ticket INC09000010 0.815.
    best_vec = max((x.vector_score or 0.0 for x in sources), default=0.0)
    gap = known_gap(query)
    low = best_vec < LOW_CONFIDENCE_BELOW or gap is not None

    allowed = []
    for src in sources:
        allowed += [src.url, src.page_url] + [l["url"] for l in src.links] + src.external_links
    allowed = list(dict.fromkeys(allowed))

    context = format_context(sources)
    timings["total"] = round((time.perf_counter() - t_start) * 1000, 1)
    return RetrievalResult(
        query=query,
        query_topics=query_topics,
        sources=sources,
        context=context,
        allowed_urls=allowed,
        low_confidence=low,
        trace={
            "timings_ms": timings,
            "candidates": counts,
            "settings": {"k": k, "candidates": candidates, "rrf_k": _RRF_K,
                         "topic_mode": _TOPIC_MODE, "topic_weight": round(topic_weight, 4),
                         "fulltext_weight": _FULLTEXT_WEIGHT, "max_per_page": _MAX_PER_PAGE,
                         "stale_after_days": _STALE_AFTER_DAYS, "stale_factor": _STALE_FACTOR,
                         "embedding_model": emb.model},
            "best_vector_score": round(best_vec, 4),
            "low_confidence_threshold": LOW_CONFIDENCE_BELOW,
            "known_gap": gap,
            "context_chars": len(context),
        },
    )


def format_context(sources: List[Source], max_chars: int = 2500) -> str:
    """The block to put in front of the LLM. URLs are shown so the model can
    reason about them, but it is told to cite by [S#] only."""
    blocks = []
    for src in sources:
        title = src.page_title if src.heading in ("Overview", src.page_title) \
            else "%s > %s" % (src.page_title, src.heading)
        lines = ["[%s] %s" % (src.ref, title), "URL: %s" % src.url]
        if src.content_type == "doc":
            # rc-docs is the technical reference; the website is announcements
            # and policy. The model should know which it is reading.
            lines.append("Source: RC documentation (rc-docs)%s" % (
                ", last updated %s" % src.last_modified if src.last_modified else ""))
        # Posts are dated announcements (purges, outages); the model needs the
        # date to tell the current one from last year's.
        if src.content_type != "page" and src.published:
            lines.append("Published: %s" % src.published)
        if src.topics:
            lines.append("Topics: %s" % ", ".join(src.topics))
        lines.append(_clip(src.text, max_chars))
        if src.external_links:
            lines.append("Linked documentation: %s" % ", ".join(src.external_links[:8]))
        if src.links:
            lines.append("Related RC pages: %s" % "; ".join(
                "%s <%s>" % (l["title"], l["url"]) for l in src.links[:6]))
        blocks.append("\n".join(lines))
    return "\n\n---\n\n".join(blocks)
