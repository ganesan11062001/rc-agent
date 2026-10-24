#!/usr/bin/env bash
# Stop the project's Neo4j. The store on /tmp is left in place so a restart
# reuses it; use --purge to delete it and force a clean rebuild.
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NEO4J_HOME="$(echo "$ROOT"/vendor/neo4j-community-*)"
DATA_DIR="${RC_KG_DATA_DIR:-/tmp/rc_kg_neo4j}"
PIDFILE="$DATA_DIR/run/neo4j.pid"

if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
  "$NEO4J_HOME/bin/neo4j" stop 2>/dev/null || kill "$(cat "$PIDFILE")"
  echo "stopped"
else
  echo "not running"
fi
rm -f "$PIDFILE"

if [ "${1:-}" = "--purge" ]; then
  rm -rf "$DATA_DIR/data" && echo "store deleted: $DATA_DIR/data"
fi
