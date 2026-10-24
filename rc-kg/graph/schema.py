"""Constraints and indexes. Idempotent -- safe to run on every ingest."""

import logging
from typing import List

from config import get_settings

from .connection import driver

logger = logging.getLogger(__name__)

CONSTRAINTS: List[str] = [
    # Page.url is the join key for every body link, so uniqueness here is what
    # stops one page becoming two nodes via a trailing-slash difference.
    "CREATE CONSTRAINT page_url IF NOT EXISTS "
    "FOR (p:Page) REQUIRE p.url IS UNIQUE",
    # wp_id is only unique *within* a content type: a page and a post can share
    # an id. A node key over both is the correct constraint.
    "CREATE CONSTRAINT page_wpid_type IF NOT EXISTS "
    "FOR (p:Page) REQUIRE (p.wp_id, p.content_type) IS UNIQUE",
    "CREATE CONSTRAINT section_id IF NOT EXISTS "
    "FOR (s:Section) REQUIRE s.section_id IS UNIQUE",
    "CREATE CONSTRAINT topic_name IF NOT EXISTS "
    "FOR (t:Topic) REQUIRE t.name IS UNIQUE",
    "CREATE CONSTRAINT category_wpid IF NOT EXISTS "
    "FOR (c:Category) REQUIRE c.wp_id IS UNIQUE",
    "CREATE CONSTRAINT extlink_url IF NOT EXISTS "
    "FOR (e:ExternalLink) REQUIRE e.url IS UNIQUE",
]

# Requirement 1: nothing enters the graph without a URL. Enforced in the
# database, not only in the writer, so a future script cannot bypass it.
EXISTENCE: List[str] = [
    "CREATE CONSTRAINT page_url_exists IF NOT EXISTS "
    "FOR (p:Page) REQUIRE p.url IS NOT NULL",
    "CREATE CONSTRAINT section_url_exists IF NOT EXISTS "
    "FOR (s:Section) REQUIRE s.url_with_anchor IS NOT NULL",
]

INDEXES: List[str] = [
    "CREATE INDEX page_content_type IF NOT EXISTS FOR (p:Page) ON (p.content_type)",
    "CREATE INDEX page_published IF NOT EXISTS FOR (p:Page) ON (p.published)",
    "CREATE INDEX section_hash IF NOT EXISTS FOR (s:Section) ON (s.content_hash)",
    "CREATE INDEX section_retrievable IF NOT EXISTS FOR (s:Section) ON (s.retrievable)",
]

FULLTEXT: List[str] = [
    "CREATE FULLTEXT INDEX topic_search IF NOT EXISTS "
    "FOR (t:Topic) ON EACH [t.name, t.aliases]",
    # Not in the original plan, but free: it is the lexical half of Retriever B
    # and costs one index on data already stored.
    "CREATE FULLTEXT INDEX section_search IF NOT EXISTS "
    "FOR (s:Section) ON EACH [s.heading, s.text]",
]


def vector_index_statement(dim: int) -> str:
    return (
        "CREATE VECTOR INDEX section_embeddings IF NOT EXISTS "
        "FOR (s:Section) ON (s.embedding) "
        "OPTIONS {indexConfig: {"
        "`vector.dimensions`: %d, "
        "`vector.similarity_function`: 'cosine'}}" % dim
    )


def apply() -> None:
    """Create everything. Existence constraints are Enterprise-only, so a
    failure there is logged and tolerated rather than fatal."""
    s = get_settings()
    with driver().session(database=s.neo4j_database) as sess:
        for stmt in CONSTRAINTS + INDEXES + FULLTEXT + [vector_index_statement(s.embedding_dim)]:
            sess.run(stmt)
        logger.info(
            "applied %d constraints, %d indexes, %d fulltext, 1 vector index",
            len(CONSTRAINTS), len(INDEXES), len(FULLTEXT),
        )
        for stmt in EXISTENCE:
            try:
                sess.run(stmt)
            except Exception as exc:              # noqa: BLE001
                logger.warning(
                    "existence constraint unavailable on this edition "
                    "(the writer enforces it anyway): %s",
                    str(exc).split("\n")[0],
                )


def drop_all_data() -> None:
    """Wipe nodes and relationships, keeping the schema. For a clean re-ingest."""
    s = get_settings()
    with driver().session(database=s.neo4j_database) as sess:
        sess.run("MATCH (n) DETACH DELETE n")
    logger.info("cleared all nodes and relationships")
