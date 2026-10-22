#!/usr/bin/env bash
# Build the knowledge graph from the live site and store it on /projects.
#
#   scripts/build_graph.sh                 build, eval-gate, publish
#   scripts/build_graph.sh --min-hit 0.8   loosen the gate
#   scripts/build_graph.sh --force         publish even if the gate fails
#
# Output, in $RC_KG_SNAPSHOT (default <repo>/data/graph):
#   neo4j.dump      the whole graph: pages, sections, embeddings, topics, indexes
#   manifest.json   when, from which commit, node/edge counts, eval scores
#   ingest_report.json
#   previous/       the snapshot this one replaced, for rollback
#
# Runs anywhere with network access to rc.northeastern.edu: a login node, an
# interactive job, or a cron'd sbatch. ~4 minutes, ~100 polite requests. A
# private Neo4j on node-local disk is used and deleted; the live one serving
# TicketMate is never touched, and a running job keeps the graph it loaded.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"

MIN_HIT=0.9
FORCE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --min-hit) MIN_HIT="$2"; shift 2 ;;
    --force)   FORCE=1; shift ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) kg_die "unknown argument $1" ;;
  esac
done

SNAP="${RC_KG_SNAPSHOT:-$RC_KG_ROOT/data/graph}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/rc_kg_build_${USER}_XXXX")"
cleanup() { kg_instance_stop; rm -rf "$WORK"; }
trap cleanup EXIT INT TERM

kg_log "building in $WORK"
kg_instance_init "$WORK/neo4j"
kg_instance_start
kg_python_env
cd "$WORK" || exit 1          # keep the repo's dev .env out of this build

# The only step that needs the Hub: make sure the embedding model is cached
# under vendor/hf, where serve_kg.sh will look for it offline.
MODEL="$("$RC_KG_PY" -c 'from config import get_settings; print(get_settings().embedding_model)')"
HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 "$RC_KG_PY" -c \
  "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" >/dev/null \
  || kg_die "could not download $MODEL into $HF_HOME"

kg_log "phase 1: ingest"
"$RC_KG_PY" -m ingest.run --report "$WORK/ingest_report.json" \
  || kg_die "ingest failed"
kg_log "phase 2: topics + embeddings"
"$RC_KG_PY" -m enrich.run || kg_die "enrich failed"
kg_log "phase 5: eval (gate: hit@5 >= $MIN_HIT)"
"$RC_KG_PY" -m eval.run --json "$WORK/eval.json" --min-hit "$MIN_HIT"
GATE=$?

"$RC_KG_PY" - "$WORK" "$MODEL" <<'PY' || kg_die "could not write manifest"
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
import config
from config import get_settings
from graph.writer import counts
work, model = Path(sys.argv[1]), sys.argv[2]
root = Path(config.__file__).resolve().parent
try:
    commit = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                            capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                                capture_output=True, text=True).stdout.strip())
except OSError:
    commit, dirty = "", False
(work / "manifest.json").write_text(json.dumps({
    "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    "git_commit": commit + ("-dirty" if dirty else ""),
    "embedding_model": model,
    "embedding_dim": get_settings().embedding_dim,
    "neo4j_version": "5.26",
    "counts": counts(),
    "eval": json.loads((work / "eval.json").read_text()),
}, indent=2))
PY

if [ "$GATE" != 0 ] && [ "$FORCE" != 1 ]; then
  kg_die "eval below the gate; snapshot NOT published (the old one is untouched). Use --force to override."
fi

kg_log "dumping"
kg_instance_stop
mkdir -p "$WORK/out"
"$NEO4J_HOME/bin/neo4j-admin" database dump neo4j --to-path="$WORK/out" \
  --overwrite-destination=true >"$WORK/dump.log" 2>&1 \
  || { cat "$WORK/dump.log" >&2; kg_die "dump failed"; }
mv "$WORK/manifest.json" "$WORK/ingest_report.json" "$WORK/out/"

# Publish: stage next to the target (same filesystem), then swap, so a job
# starting mid-publish never loads a half-copied dump.
mkdir -p "$SNAP" || kg_die "cannot create $SNAP"
STAGE="$SNAP/.staging.$$"
rm -rf "$STAGE"; cp -r "$WORK/out" "$STAGE" || kg_die "copy to $SNAP failed"
chmod -R a+rX "$STAGE"        # other users' TicketMate jobs load it too
if [ -f "$SNAP/neo4j.dump" ]; then
  # Hard links: the current files stay in place until renamed over below.
  rm -rf "$SNAP/previous"; mkdir -p "$SNAP/previous"
  for f in neo4j.dump manifest.json ingest_report.json; do
    [ -f "$SNAP/$f" ] && ln -f "$SNAP/$f" "$SNAP/previous/$f"
  done
fi
# Each rename is atomic. A job starting between them loads the old dump with
# the new manifest beside it, which only affects what /health reports.
for f in ingest_report.json manifest.json neo4j.dump; do
  mv -f "$STAGE/$f" "$SNAP/$f" || kg_die "publishing $f failed"
done
rmdir "$STAGE"

kg_log "published to $SNAP"
ls -lh "$SNAP"
"$RC_KG_PY" -c "import json; m=json.load(open('$SNAP/manifest.json')); print('built', m['built_at'], 'commit', m['git_commit']); print('eval', m['eval'])"
