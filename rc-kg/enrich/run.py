"""Phase 2: topics and embeddings, on top of the graph Phase 1 wrote.

Reads sections from Neo4j rather than re-fetching the site, so it can be
re-run as often as topics.yaml or the embedding model changes. Embedding is
incremental: a section is re-embedded only when its text or the model changed,
recorded as `embedded_key` = model + content_hash.

Run:  python -m enrich.run            # topics + any stale embeddings
      python -m enrich.run --reembed  # embed everything again
"""

import argparse
import logging
import sys
import time
from typing import Dict, List

from config import get_settings
from graph import schema
from graph.connection import driver
from retrieval.embeddings import get_embedder

from .topics import load_topics, match_topics

logger = logging.getLogger("enrich")

_BATCH = 64

READ_SECTIONS = """
MATCH (p:Page)-[:HAS_SECTION]->(s:Section)
RETURN s.section_id AS sid, s.heading AS heading, s.text AS text,
       s.content_hash AS hash, s.embedded_key AS embedded_key,
       p.title AS title
"""

UPSERT_TOPICS = """
UNWIND $rows AS r
MERGE (t:Topic {name: r.name})
SET t.description = r.description, t.aliases = r.aliases
"""

# Topics no longer in topics.yaml are removed, so the file stays the only
# source of truth.
DROP_STALE_TOPICS = "MATCH (t:Topic) WHERE NOT t.name IN $names DETACH DELETE t"

CLEAR_MENTIONS = "MATCH (:Section)-[m:MENTIONS]->(:Topic) DELETE m"

LINK_MENTIONS = """
UNWIND $rows AS r
MATCH (s:Section {section_id: r.sid})
MATCH (t:Topic {name: r.topic})
MERGE (s)-[m:MENTIONS]->(t)
SET m.hits = r.hits, m.in_heading = r.in_heading
"""

WRITE_EMBEDDINGS = """
UNWIND $rows AS r
MATCH (s:Section {section_id: r.sid})
CALL db.create.setNodeVectorProperty(s, 'embedding', r.vec)
SET s.embedded_key = r.key, s.embedding_model = r.model
"""


def embedding_text(title: str, heading: str, text: str) -> str:
    """What gets embedded: the section in its page's context.

    A section headed "Solution:" or "Overview" means nothing on its own; the
    page title is what tells the model it is about OOM kills or AICR.
    """
    head = title if heading in ("", "Overview") or heading == title else "%s > %s" % (title, heading)
    return "%s\n%s" % (head, text)


def _run_batched(sess, stmt: str, rows: List[Dict]) -> None:
    for i in range(0, len(rows), _BATCH):
        sess.run(stmt, rows=rows[i : i + _BATCH])


def enrich(reembed: bool = False) -> Dict[str, int]:
    s = get_settings()
    schema.apply()
    topics = load_topics()

    with driver().session(database=s.neo4j_database) as sess:
        sections = [dict(r) for r in sess.run(READ_SECTIONS)]
        if not sections:
            raise RuntimeError("no sections in the graph -- run `python -m ingest.run` first")

        # --- topics -------------------------------------------------------
        sess.run(UPSERT_TOPICS, rows=[
            {"name": t.name, "description": t.description, "aliases": t.aliases}
            for t in topics
        ])
        sess.run(DROP_STALE_TOPICS, names=[t.name for t in topics])
        sess.run(CLEAR_MENTIONS)
        mention_rows = []
        for sec in sections:
            heading_hits = match_topics(sec["heading"] or "", topics)
            for name, hits in match_topics(
                "%s\n%s" % (sec["heading"] or "", sec["text"] or ""), topics
            ).items():
                mention_rows.append({
                    "sid": sec["sid"], "topic": name, "hits": hits,
                    "in_heading": name in heading_hits,
                })
        _run_batched(sess, LINK_MENTIONS, mention_rows)
        logger.info("%d topics, %d MENTIONS edges", len(topics), len(mention_rows))

        # --- embeddings ---------------------------------------------------
        emb = get_embedder()
        todo = [
            sec for sec in sections
            if reembed or sec["embedded_key"] != "%s:%s" % (emb.model, sec["hash"])
        ]
        t0 = time.monotonic()
        for i in range(0, len(todo), _BATCH):
            batch = todo[i : i + _BATCH]
            vecs = emb.embed_documents([
                embedding_text(b["title"] or "", b["heading"] or "", b["text"] or "")
                for b in batch
            ])
            sess.run(WRITE_EMBEDDINGS, rows=[
                {"sid": b["sid"], "vec": v, "model": emb.model,
                 "key": "%s:%s" % (emb.model, b["hash"])}
                for b, v in zip(batch, vecs)
            ])
            logger.info("embedded %d/%d", min(i + _BATCH, len(todo)), len(todo))
        logger.info("embedded %d section(s) in %.1fs (%d already current)",
                    len(todo), time.monotonic() - t0, len(sections) - len(todo))

        untagged = sess.run(
            "MATCH (s:Section {retrievable: true}) WHERE NOT (s)-[:MENTIONS]->() "
            "RETURN count(s) AS c"
        ).single()["c"]

    return {"topics": len(topics), "mentions": len(mention_rows),
            "embedded": len(todo), "sections": len(sections),
            "retrievable_without_topic": untagged}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--reembed", action="store_true", help="ignore embedded_key")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    print(enrich(reembed=args.reembed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
