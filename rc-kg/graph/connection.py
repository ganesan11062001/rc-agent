"""Neo4j driver, one per process -- and a live switch to a newer graph.

A session serves the snapshot it started with; when a newer snapshot is
published, scripts/refresh_kg.sh loads it into a second private Neo4j and
asks the API to switch (`POST /admin/swap`, api/app.py), which calls `swap`
here. The new driver is checked -- it connects and the graph has embedded
sections -- before it replaces the old one, so a bad graph is never switched
to; queries already running on the old driver finish, because it is closed
only after a grace period.
"""

import logging
import threading
from typing import Dict, Optional

from neo4j import Driver, GraphDatabase

from config import get_settings

logger = logging.getLogger(__name__)

_driver: Optional[Driver] = None
_lock = threading.Lock()
SWAP_GRACE_S = 60          # how long the old driver stays open for queries already running
current: Dict[str, Optional[str]] = {"uri": None, "manifest": None}


def driver() -> Driver:
    """The shared Neo4j driver, created and connectivity-checked on first call.
    Raises if NEO4J_PASSWORD is not set."""
    global _driver
    if _driver is None:
        with _lock:
            if _driver is None:
                s = get_settings()
                if not s.neo4j_password:
                    raise RuntimeError(
                        "NEO4J_PASSWORD is not set. Run scripts/start_neo4j.sh, which "
                        "writes the generated password into .env."
                    )
                d = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
                d.verify_connectivity()
                _driver = d
                current["uri"] = s.neo4j_uri
                logger.info("connected to Neo4j at %s", s.neo4j_uri)
    return _driver


def swap(uri: str, user: str, password: str, manifest: Optional[str] = None) -> Dict[str, object]:
    """Switch to the Neo4j at `uri` once it answers and has embedded sections.
    Returns {"sections": n, "uri": uri}. Raises (and keeps the old driver) if
    the new graph is not usable."""
    global _driver
    new = GraphDatabase.driver(uri, auth=(user, password))
    try:
        new.verify_connectivity()
        with new.session(database=get_settings().neo4j_database) as sess:
            n = sess.run("MATCH (s:Section) WHERE s.embedding IS NOT NULL RETURN count(s) AS n").single()["n"]
        if not n:
            raise RuntimeError("the new graph has no embedded sections")
    except Exception:
        new.close()
        raise
    with _lock:
        old, _driver = _driver, new
        current.update(uri=uri, manifest=manifest)
    if old is not None:
        threading.Timer(SWAP_GRACE_S, old.close).start()
    logger.info("switched to Neo4j at %s (%d embedded sections)", uri, n)
    return {"sections": n, "uri": uri}


def close() -> None:
    """Close the shared driver, if open; the next driver() call reconnects."""
    global _driver
    with _lock:
        if _driver is not None:
            _driver.close()
            _driver = None
