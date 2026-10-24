"""Write pages, sections and edges into Neo4j.

Everything is MERGE-on-key so a re-ingest updates in place rather than
duplicating. Writes are batched with UNWIND: 104 pages and ~380 sections is
small, but one round trip per node is still 500 round trips for no reason.
"""

import logging
from typing import Dict, Iterable, List, Optional

from config import get_settings

from .connection import driver

logger = logging.getLogger(__name__)

_BATCH = 200


def _chunks(rows: List[Dict], size: int = _BATCH) -> Iterable[List[Dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _run(stmt: str, rows: List[Dict]) -> int:
    if not rows:
        return 0
    s = get_settings()
    written = 0
    with driver().session(database=s.neo4j_database) as sess:
        for batch in _chunks(rows):
            sess.run(stmt, rows=batch)
            written += len(batch)
    return written


UPSERT_PAGES = """
UNWIND $rows AS r
MERGE (p:Page {url: r.url})
SET p.wp_id         = r.wp_id,
    p.content_type  = r.content_type,
    p.title         = r.title,
    p.published     = r.published,
    p.last_modified = r.last_modified,
    p.slug          = r.slug,
    p.source        = r.source,
    p.site          = r.site
"""

UPSERT_SECTIONS = """
UNWIND $rows AS r
MATCH (p:Page {url: r.page_url})
MERGE (s:Section {section_id: r.section_id})
SET s.heading          = r.heading,
    s.heading_level    = r.heading_level,
    s.text             = r.text,
    s.url_with_anchor  = r.url_with_anchor,
    s.anchor_kind      = r.anchor_kind,
    s.content_hash     = r.content_hash,
    s.order            = r.order,
    s.char_count       = r.char_count,
    s.retrievable      = r.retrievable,
    s.duplicate_of     = r.duplicate_of
MERGE (p)-[:HAS_SECTION]->(s)
"""

LINK_NEXT = """
UNWIND $rows AS r
MATCH (a:Section {section_id: r.from_id})
MATCH (b:Section {section_id: r.to_id})
MERGE (a)-[:NEXT]->(b)
"""

LINK_CHILD_OF = """
UNWIND $rows AS r
MATCH (c:Page {url: r.child_url})
MATCH (p:Page {url: r.parent_url})
MERGE (c)-[:CHILD_OF]->(p)
"""

UPSERT_CATEGORIES = """
UNWIND $rows AS r
MERGE (c:Category {wp_id: r.wp_id})
SET c.name = r.name, c.taxonomy = r.taxonomy
"""

LINK_IN_CATEGORY = """
UNWIND $rows AS r
MATCH (p:Page {url: r.page_url})
MATCH (c:Category {wp_id: r.category_id})
MERGE (p)-[:IN_CATEGORY]->(c)
"""

LINK_TO_PAGE = """
UNWIND $rows AS r
MATCH (s:Section {section_id: r.section_id})
MATCH (p:Page {url: r.target_url})
MERGE (s)-[rel:LINKS_TO]->(p)
SET rel.href_as_written = r.href_as_written,
    rel.via_redirect    = r.via_redirect
"""

UPSERT_EXTERNAL = """
UNWIND $rows AS r
MERGE (e:ExternalLink {url: r.url})
SET e.domain = r.domain
"""

LINK_OUT = """
UNWIND $rows AS r
MATCH (s:Section {section_id: r.section_id})
MATCH (e:ExternalLink {url: r.url})
MERGE (s)-[:LINKS_OUT]->(e)
"""


def upsert_pages(rows: List[Dict]) -> int:
    missing = [r for r in rows if not r.get("url")]
    if missing:
        # Requirement 1, enforced here because Community Edition has no
        # existence constraints.
        raise ValueError("refusing to write %d Page row(s) with no url" % len(missing))
    n = _run(UPSERT_PAGES, rows)
    logger.info("pages upserted: %d", n)
    return n


def upsert_sections(rows: List[Dict]) -> int:
    missing = [r for r in rows if not r.get("url_with_anchor")]
    if missing:
        raise ValueError(
            "refusing to write %d Section row(s) with no url_with_anchor" % len(missing)
        )
    n = _run(UPSERT_SECTIONS, rows)
    logger.info("sections upserted: %d", n)
    return n


def link_next(rows: List[Dict]) -> int:
    return _run(LINK_NEXT, rows)


def link_child_of(rows: List[Dict]) -> int:
    return _run(LINK_CHILD_OF, rows)


def upsert_categories(rows: List[Dict]) -> int:
    return _run(UPSERT_CATEGORIES, rows)


def link_in_category(rows: List[Dict]) -> int:
    return _run(LINK_IN_CATEGORY, rows)


def link_to_pages(rows: List[Dict]) -> int:
    return _run(LINK_TO_PAGE, rows)


def upsert_external(rows: List[Dict]) -> int:
    return _run(UPSERT_EXTERNAL, rows)


def link_out(rows: List[Dict]) -> int:
    return _run(LINK_OUT, rows)


def delete_pages_not_in(urls: List[str]) -> int:
    """Phase 4: remove pages that are no longer published, and their sections."""
    s = get_settings()
    with driver().session(database=s.neo4j_database) as sess:
        rec = sess.run(
            "MATCH (p:Page) WHERE NOT p.url IN $urls "
            "OPTIONAL MATCH (p)-[:HAS_SECTION]->(sec:Section) "
            "WITH collect(DISTINCT p) AS ps, collect(DISTINCT sec) AS ss "
            "FOREACH (x IN ss | DETACH DELETE x) "
            "FOREACH (x IN ps | DETACH DELETE x) "
            "RETURN size(ps) AS pages, size(ss) AS sections",
            urls=urls,
        ).single()
    if rec and rec["pages"]:
        logger.info("deleted %d stale page(s), %d section(s)", rec["pages"], rec["sections"])
    return rec["pages"] if rec else 0


def counts() -> Dict[str, int]:
    """Node and relationship census, for the report."""
    s = get_settings()
    out: Dict[str, int] = {}
    with driver().session(database=s.neo4j_database) as sess:
        for label in ("Page", "Section", "Topic", "Category", "ExternalLink"):
            out[label] = sess.run(
                "MATCH (n:%s) RETURN count(n) AS c" % label
            ).single()["c"]
        for rel in ("HAS_SECTION", "CHILD_OF", "IN_CATEGORY", "NEXT",
                    "LINKS_TO", "LINKS_OUT", "MENTIONS"):
            out[rel] = sess.run(
                "MATCH ()-[r:%s]->() RETURN count(r) AS c" % rel
            ).single()["c"]
    return out
