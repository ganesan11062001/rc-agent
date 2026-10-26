"""Write pages, sections and edges into Neo4j.

Everything is MERGE-on-key so a re-ingest updates in place rather than
duplicating. Writes are batched with UNWIND: ~170 pages and ~600 sections is
small, but one round trip per node is still ~800 round trips for no reason.
"""

import logging
from typing import Dict, Iterable, List

from config import get_settings

from .connection import driver

logger = logging.getLogger(__name__)

_BATCH = 200


def _chunks(rows: List[Dict], size: int = _BATCH) -> Iterable[List[Dict]]:
    for i in range(0, len(rows), size):
        yield rows[i : i + size]


def _run(stmt: str, rows: List[Dict]) -> int:
    """Run an UNWIND $rows statement over `rows` in batches; returns the row count."""
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
    """MERGE Page nodes by url and set their metadata; raises if any row has no url."""
    missing = [r for r in rows if not r.get("url")]
    if missing:
        # Requirement 1, enforced here because Community Edition has no
        # existence constraints.
        raise ValueError("refusing to write %d Page row(s) with no url" % len(missing))
    n = _run(UPSERT_PAGES, rows)
    logger.info("pages upserted: %d", n)
    return n


def upsert_sections(rows: List[Dict]) -> int:
    """MERGE Section nodes by section_id and link them to their Page; raises
    if any row has no url_with_anchor."""
    missing = [r for r in rows if not r.get("url_with_anchor")]
    if missing:
        raise ValueError(
            "refusing to write %d Section row(s) with no url_with_anchor" % len(missing)
        )
    n = _run(UPSERT_SECTIONS, rows)
    logger.info("sections upserted: %d", n)
    return n


def link_next(rows: List[Dict]) -> int:
    """Section -[:NEXT]-> Section, in page order (rows: from_id, to_id)."""
    return _run(LINK_NEXT, rows)


def link_child_of(rows: List[Dict]) -> int:
    """Page -[:CHILD_OF]-> parent Page (rows: child_url, parent_url)."""
    return _run(LINK_CHILD_OF, rows)


def upsert_categories(rows: List[Dict]) -> int:
    """MERGE Category nodes by WordPress id (rows: wp_id, name, taxonomy)."""
    return _run(UPSERT_CATEGORIES, rows)


def link_in_category(rows: List[Dict]) -> int:
    """Page -[:IN_CATEGORY]-> Category (rows: page_url, category_id)."""
    return _run(LINK_IN_CATEGORY, rows)


def link_to_pages(rows: List[Dict]) -> int:
    """Section -[:LINKS_TO]-> internal Page, keeping the href as written and
    whether it went through a redirect."""
    return _run(LINK_TO_PAGE, rows)


def upsert_external(rows: List[Dict]) -> int:
    """MERGE ExternalLink nodes by url (rows: url, domain)."""
    return _run(UPSERT_EXTERNAL, rows)


def link_out(rows: List[Dict]) -> int:
    """Section -[:LINKS_OUT]-> ExternalLink (rows: section_id, url)."""
    return _run(LINK_OUT, rows)



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
