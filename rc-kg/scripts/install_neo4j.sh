#!/usr/bin/env bash
# Fetch the Neo4j tarball into vendor/. Separate from start_neo4j.sh so a
# restart never depends on the network.
set -uo pipefail
V="${NEO4J_VERSION:-5.26.0}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
mkdir -p "$ROOT/vendor"
if [ -d "$ROOT/vendor/neo4j-community-$V" ]; then
  echo "already present: vendor/neo4j-community-$V"; exit 0
fi
echo "downloading Neo4j $V (~150 MB)..."
curl -fSL --max-time 600 -o "$ROOT/vendor/neo4j.tar.gz" \
  "https://dist.neo4j.org/neo4j-community-${V}-unix.tar.gz" || {
    echo "download failed" >&2; exit 1; }
tar xzf "$ROOT/vendor/neo4j.tar.gz" -C "$ROOT/vendor" && rm -f "$ROOT/vendor/neo4j.tar.gz"
"$ROOT/vendor/neo4j-community-$V/bin/neo4j" version
