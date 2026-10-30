#!/usr/bin/env bash
# Build the knowledge graph from the live sites and store it on /projects.
#
#   scripts/build_graph.sh                     the full graph: website + rc-docs
#   scripts/build_graph.sh --variant website   the RC website only
#   scripts/build_graph.sh --all               both variants, one after the other
#   scripts/build_graph.sh --min-hit 0.8       loosen the gate
#   scripts/build_graph.sh --force             publish even if the gate fails
#
# Each variant has its own snapshot, eval gate and rollback copy:
#   full     data/graph/            gated on all eval questions
#   website  data/graph-website/    gated on the questions the website can answer
#
# Output, in $RC_KG_SNAPSHOT (default: the variant's directory above):
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
VARIANT=full
ALL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --min-hit) MIN_HIT="$2"; shift 2 ;;
    --force)   FORCE=1; shift ;;
    --variant) VARIANT="$2"; shift 2 ;;
    --all)     ALL=1; shift ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) kg_die "unknown argument $1" ;;
  esac
done

if [ "$ALL" = 1 ]; then
  [ -z "${RC_KG_SNAPSHOT:-}" ] || kg_die "--all with RC_KG_SNAPSHOT would publish every variant to one directory"
  # Each variant in its own process, so one failing gate does not stop the
  # other; the exit status reports any failure.
  rc=0
  for v in $KG_VARIANTS; do
    "$0" --variant "$v" --min-hit "$MIN_HIT" $([ "$FORCE" = 1 ] && echo --force) || rc=1
  done
  exit $rc
fi

SNAP="${RC_KG_SNAPSHOT:-$(kg_snapshot_dir "$VARIANT")}" || exit 1
kg_variant_env "$VARIANT"
SITES="$(kg_variant_sites "$VARIANT")"
kg_log "variant: $VARIANT ($SITES) -> $SNAP"
kg_sweep_stale
WORK="$(kg_new_dir build)" || exit 1
kg_watchdog "$WORK" & WD_PID=$!
# On any exit: stop the watchdog and this build's Neo4j, then drop its work dir.
cleanup() { kill "$WD_PID" 2>/dev/null; kg_instance_stop; rm -rf "$WORK"; }
trap cleanup EXIT
trap 'exit 143' INT TERM

kg_log "building in $WORK"
kg_instance_init "$WORK/neo4j"
kg_instance_start
kg_python_env
cd "$WORK" || exit 1          # keep the repo's dev .env out of this build

# The only step that needs the Hub: make sure the embedding model is cached
# under vendor/hf, where serve_kg.sh will look for it offline.
MODEL="$("$RC_KG_PY" -c 'from config import get_settings; print(get_settings().embedding_model)')"
# Offline first: a session's background rebuild (refresh_kg.sh) runs on a
# compute node, which reaches the Hub only through a flaky proxy.
HF_HUB_OFFLINE=1 "$RC_KG_PY" -c \
  "from huggingface_hub import snapshot_download; snapshot_download('$MODEL', local_files_only=True)" >/dev/null 2>&1 \
|| HF_HUB_OFFLINE=0 TRANSFORMERS_OFFLINE=0 "$RC_KG_PY" -c \
  "from huggingface_hub import snapshot_download; snapshot_download('$MODEL')" >/dev/null \
  || kg_die "could not download $MODEL into $HF_HOME"

kg_log "phase 1: ingest"
"$RC_KG_PY" -m ingest.run --report "$WORK/ingest_report.json" \
  || kg_die "ingest failed"
kg_log "phase 2: topics + embeddings"
"$RC_KG_PY" -m enrich.run || kg_die "enrich failed"
kg_log "phase 5: eval (gate: hit@5 >= $MIN_HIT)"
"$RC_KG_PY" -m eval.run --json "$WORK/eval.json" --min-hit "$MIN_HIT" --sites "$SITES"
GATE=$?
# Full Tier 1 report (RAGAS context metrics + ranking metrics, with CIs) and
# the graph's integrity checks, kept beside the snapshot. A graph whose
# relations break an invariant is never published, whatever --force says.
"$RC_KG_PY" -m eval.retrieval_eval --sites "$SITES" --json "$WORK/retrieval_eval.json" >/dev/null \
  || kg_die "retrieval_eval failed"
kg_log "graph evaluation (integrity is a hard gate)"
"$RC_KG_PY" -m eval.graph_eval --sites "$SITES" --json "$WORK/graph_eval.json" | sed -n '/1. INTEGRITY/,/3. USEFULNESS/p' | grep -E "FAIL|INTEGRITY"
[ "${PIPESTATUS[0]}" = 0 ] || kg_die "graph integrity checks failed; snapshot NOT published (see graph_eval.json)"

"$RC_KG_PY" - "$WORK" "$MODEL" "$VARIANT" "$SITES" <<'PY' || kg_die "could not write manifest"
import json, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path
import config
from config import get_settings
from graph.writer import counts
work, model, variant, sites = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4].split(",")
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
    "variant": variant,
    "sites": sites,
    "embedding_model": model,
    "embedding_dim": get_settings().embedding_dim,
    "neo4j_version": "5.26",
    "counts": counts(),
    "eval": json.loads((work / "eval.json").read_text()),
    "ragas": {k: v["mean"] for k, v in json.loads((work / "retrieval_eval.json").read_text())["overall"].items()
              if k in ("context_precision", "id_context_precision", "id_context_recall", "ndcg@5")},
    "graph_integrity": all(c["ok"] for c in json.loads((work / "graph_eval.json").read_text())["integrity"]),
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
mv "$WORK/manifest.json" "$WORK/ingest_report.json" "$WORK/retrieval_eval.json" "$WORK/graph_eval.json" "$WORK/out/"

# Publish: stage next to the target (same filesystem), then swap, so a job
# starting mid-publish never loads a half-copied dump.
mkdir -p "$SNAP" || kg_die "cannot create $SNAP"
STAGE="$SNAP/.staging.$$"
rm -rf "$STAGE"; cp -r "$WORK/out" "$STAGE" || kg_die "copy to $SNAP failed"
chmod -R a+rX "$STAGE"        # other users' TicketMate jobs load it too
if [ -f "$SNAP/neo4j.dump" ]; then
  # Hard links: the current files stay in place until renamed over below.
  rm -rf "$SNAP/previous"; mkdir -p "$SNAP/previous"
  for f in neo4j.dump manifest.json ingest_report.json retrieval_eval.json graph_eval.json; do
    [ -f "$SNAP/$f" ] && ln -f "$SNAP/$f" "$SNAP/previous/$f"
  done
fi
# Each rename is atomic. A job starting between them loads the old dump with
# the new manifest beside it, which only affects what /health reports.
for f in ingest_report.json retrieval_eval.json graph_eval.json manifest.json neo4j.dump; do
  mv -f "$STAGE/$f" "$SNAP/$f" || kg_die "publishing $f failed"
done
rmdir "$STAGE"

kg_log "published to $SNAP"
ls -lh "$SNAP"
"$RC_KG_PY" -c "import json; m=json.load(open('$SNAP/manifest.json')); print('built', m['built_at'], 'commit', m['git_commit']); print('eval', m['eval'])"

# Keep the serving runtime in step with the venv (a no-op unless packages
# changed; see build_runtime.sh).
"$RC_KG_ROOT/scripts/build_runtime.sh" || kg_log "WARNING: runtime archive not refreshed; serving will import from /projects"
