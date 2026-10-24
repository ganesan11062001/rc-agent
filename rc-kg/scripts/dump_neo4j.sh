#!/usr/bin/env bash
# Copy the node-local store to /projects so it survives the node.
# Restore with: neo4j-admin database load neo4j --from-path=<dir> --overwrite-destination=true
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NEO4J_HOME="$(echo "$ROOT"/vendor/neo4j-community-*)"
OUT="${1:-$ROOT/data/dumps}"
mkdir -p "$OUT"
echo "Neo4j must be stopped to dump. Stopping..."
"$ROOT/scripts/stop_neo4j.sh" >/dev/null
"$NEO4J_HOME/bin/neo4j-admin" database dump neo4j --to-path="$OUT" --overwrite-destination=true
ls -lh "$OUT"
