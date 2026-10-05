"""Neo4j driver, one per process."""

import logging
from typing import Optional

from neo4j import Driver, GraphDatabase

from config import get_settings

logger = logging.getLogger(__name__)

_driver: Optional[Driver] = None


def driver() -> Driver:
    global _driver
    if _driver is None:
        s = get_settings()
        if not s.neo4j_password:
            raise RuntimeError(
                "NEO4J_PASSWORD is not set. Run scripts/start_neo4j.sh, which "
                "writes the generated password into .env."
            )
        _driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password))
        _driver.verify_connectivity()
        logger.info("connected to Neo4j at %s", s.neo4j_uri)
    return _driver


def close() -> None:
    global _driver
    if _driver is not None:
        _driver.close()
        _driver = None
