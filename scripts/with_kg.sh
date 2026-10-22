#!/usr/bin/env bash
# Run a Python module or script against a private copy of the graph snapshot.
#
#   scripts/with_kg.sh -m eval.run --all
#   scripts/with_kg.sh -m eval.citation_review
#   scripts/with_kg.sh -m retrieval.query "my job was killed"
#
# Same private, node-local Neo4j as serve_kg.sh, gone when the command exits.
# For anything that needs the graph without a running API.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"
SNAP="${RC_KG_SNAPSHOT:-$RC_KG_ROOT/data/graph}"
INST="$(mktemp -d "${RC_KG_TMP:-${TMPDIR:-/tmp}}/rc_kg_${USER}_${SLURM_JOB_ID:-local}_XXXX")" \
  || kg_die "cannot create an instance dir"
trap 'kg_instance_stop; rm -rf "$INST"' EXIT
trap 'exit 143' INT TERM
kg_instance_init "$INST"
kg_python_env
kg_instance_load "$SNAP"
kg_instance_start
"$RC_KG_PY" "$@"
