#!/usr/bin/env bash
# Serve the knowledge graph for one job: private Neo4j + the rc-kg API.
#
#   scripts/serve_kg.sh --port 8090                    foreground; Ctrl-C / SIGTERM stops both
#   scripts/serve_kg.sh --port 8090 --variant website  the website-only graph
#
# Environment (all optional):
#   RC_KG_VARIANT      full (default) | website  -- which graph to serve
#   RC_KG_SNAPSHOT     dir with neo4j.dump      default: the variant's directory
#   RC_KG_API_TOKEN    bearer token for the API (recommended on shared nodes)
#   RC_KG_TMP          node-local parent dir    default $TMPDIR, else /tmp
#   RC_KG_THREADS      CPU threads for query embedding, default 2
#   RC_KG_PROGRESS_DIR startup checklist, one <step>.json per step (see _kg_instance.sh)
#
# Cleanup: a normal stop (SIGTERM, Ctrl-C, the job ending) removes everything.
# If this script is killed outright, a watchdog stops Neo4j and the API and
# removes the dir; anything that survives even that (the watchdog killed too)
# is swept by the next start on the node. scripts/kg_ps.sh lists instances.
#
# Works on any node that can read /projects: nothing is written outside the
# instance dir, which is removed on exit. With no snapshot it falls back to
# building one from the live site (~4 min, needs network) so a fresh checkout
# still comes up -- but run scripts/build_graph.sh once instead.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"

PORT="${RC_KG_PORT:-}"
HOST=127.0.0.1
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --host) HOST="$2"; shift 2 ;;
    --variant) RC_KG_VARIANT="$2"; shift 2 ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) kg_die "unknown argument $1" ;;
  esac
done
[ -n "$PORT" ] || PORT="$(kg_free_port)"

# The checklist exists from the first second, so a client never sees nothing.
for step in prepare runtime neo4j_load neo4j_start api_start embedder graph warmup; do kg_progress "$step" pending; done
kg_progress prepare running "checking the graph snapshot"

VARIANT="${RC_KG_VARIANT:-full}"
SNAP="${RC_KG_SNAPSHOT:-$(kg_snapshot_dir "$VARIANT")}" || exit 1
# A snapshot says which variant it is; refuse to serve the wrong one quietly.
if [ -f "$SNAP/manifest.json" ]; then
  built="$("$RC_KG_PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('variant','full'))" "$SNAP/manifest.json")"
  [ "$built" = "$VARIANT" ] || kg_die "$SNAP holds the '$built' graph, not '$VARIANT'"
fi
kg_variant_env "$VARIANT"
kg_log "variant: $VARIANT -> $SNAP"
kg_sweep_stale
# Always a fresh directory this script created, so removing it on exit can
# never take anything else with it.
INST="$(kg_new_dir "${SLURM_JOB_ID:-local}")" || exit 1

API_PID=""
WD_PID=""
cleanup() {
  [ -n "$WD_PID" ] && kill "$WD_PID" 2>/dev/null
  [ -n "$API_PID" ] && kill "$API_PID" 2>/dev/null && wait "$API_PID" 2>/dev/null
  kg_instance_stop
  rm -rf "$INST"
}
trap cleanup EXIT
trap 'exit 143' INT TERM

kg_progress prepare running "creating a private Neo4j for this session"
kg_instance_init "$INST"
kg_progress prepare done
kg_watchdog "$INST" &
WD_PID=$!
kg_python_env
cd "$INST" || exit 1          # no repo .env: this process is configured by env only

kg_progress runtime running "unpacking the Python runtime (first start on this node only)"
t0=$(date +%s)
kg_runtime
kg_progress runtime done "ready in $(( $(date +%s) - t0 ))s"
[ -f "$SNAP/manifest.json" ] && export RC_KG_MANIFEST="$SNAP/manifest.json"
[ -n "${RC_KG_API_TOKEN:-}" ] || kg_log "WARNING: RC_KG_API_TOKEN unset; the API is open to every user on $(hostname -s)"

# The API starts first so its imports (~10 s) overlap Neo4j's load (~20 s);
# it answers /health with "loading" until the graph is reachable and embedded.
kg_log "rc-kg API on http://$HOST:$PORT"
kg_progress api_start running "loading Python libraries"
"${KG_PYTHON[@]}" -m uvicorn api.app:app --app-dir "$RC_KG_ROOT" \
  --host "$HOST" --port "$PORT" --log-level warning &
API_PID=$!

if [ -f "$SNAP/neo4j.dump" ]; then
  kg_log "loading snapshot $SNAP ($(du -h "$SNAP/neo4j.dump" | cut -f1))"
  kg_progress neo4j_load running "loading the $VARIANT graph snapshot ($(du -h "$SNAP/neo4j.dump" | cut -f1))"
  kg_instance_load "$SNAP"
  kg_progress neo4j_load done
  kg_progress neo4j_start running "starting Neo4j"
  kg_instance_start
  kg_progress neo4j_start done
else
  kg_log "WARNING: no snapshot at $SNAP; building from the live site (~4 min)"
  kg_progress neo4j_load running "no snapshot: building the graph from the live sites (~4 min)"
  kg_progress neo4j_start running "starting Neo4j"
  kg_instance_start
  kg_progress neo4j_start done
  HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 "${KG_PYTHON[@]}" -m ingest.run \
    --report "$INST/ingest_report.json" || kg_die "ingest failed"
  HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 "${KG_PYTHON[@]}" -m enrich.run \
    || kg_die "enrich failed"
  kg_progress neo4j_load done "built from the live sites"
fi
kg_log "neo4j up on $NEO4J_URI"
wait "$API_PID"
rc=$?
[ "$rc" = 0 ] || kg_progress api_start failed "the rc-kg API exited with status $rc"
exit $rc
