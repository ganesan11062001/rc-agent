#!/usr/bin/env bash
# Keep a running session's knowledge graph current, without stopping it.
#
# Started in the background by serve_kg.sh once the session is serving its
# snapshot; runs until the session ends. Every RC_KG_REFRESH_CHECK_MIN minutes:
#
#   1. A newer snapshot has been published (by any session, either app, or
#      rebuild.sbatch)?  Load it into a second private Neo4j, ask the API to
#      switch to it (POST /admin/swap -- it checks the new graph first and lets
#      running queries finish), then stop the Neo4j it replaced.
#   2. Otherwise, is the published snapshot older than RC_KG_REFRESH_MAX_AGE_H
#      hours? Take the shared build lock -- one build at a time across every
#      session and both apps -- rebuild from the live websites
#      (scripts/build_graph.sh: crawl, embed, eval gate, atomic publish), then
#      switch as in 1. A build that fails the eval gate publishes nothing.
#
# Nothing here can take the session's graph away: every failure leaves the
# current graph serving, and is logged.
#
#   RC_KG_REFRESH            on (default) | off       off: never build; still switch to newer snapshots
#   RC_KG_REFRESH_MAX_AGE_H  24                        rebuild when the snapshot is older than this
#   RC_KG_REFRESH_CHECK_MIN  20                        how often to check
#   RC_KG_REFRESH_FIRST_MIN  2                         first check, after startup has settled
#   RC_KG_REFRESH_LOG        <progress dir>/refresh.log, else <serving dir>/refresh.log
#
# Usage (serve_kg.sh does this): refresh_kg.sh <api port> <variant> <serving instance dir> <served built_at>
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"

PORT="$1"; VARIANT="$2"; SERVING_DIR="$3"; LOADED="${4:-}"
SNAP="$(kg_snapshot_dir "$VARIANT")" || exit 1
ROOT="$(kg_data_root)"
LOCK="$ROOT/.build-$VARIANT.lock"
LOG="${RC_KG_REFRESH_LOG:-${RC_KG_PROGRESS_DIR:-$SERVING_DIR}/refresh.log}"
MAX_AGE_H="${RC_KG_REFRESH_MAX_AGE_H:-24}"
CHECK_S=$(( ${RC_KG_REFRESH_CHECK_MIN:-20} * 60 ))
FIRST_S=$(( ${RC_KG_REFRESH_FIRST_MIN:-2} * 60 ))
LOCK_STALE_S=$(( 4 * 3600 ))          # a build lock older than this was left by a dead build
PARENT=$PPID
MINE=""                               # the instance this script started and is now serving from

log() { echo "[refresh $(date '+%F %T')] $*" >> "$LOG"; }

built_at() {
  "$RC_KG_PY" -c "import json,sys; print(json.load(open(sys.argv[1])).get('built_at',''))" "$1" 2>/dev/null
}

age_h() {   # hours since an ISO timestamp
  "$RC_KG_PY" -c "
import sys; from datetime import datetime, timezone
t = datetime.fromisoformat(sys.argv[1])
print(int((datetime.now(timezone.utc) - t).total_seconds() // 3600))" "$1" 2>/dev/null || echo 0
}

stop_instance() {   # stop the Neo4j of an instance dir (and remove the dir if this script made it)
  local dir="$1" remove="$2"
  [ -d "$dir/conf" ] && NEO4J_CONF="$dir/conf" "$NEO4J_HOME/bin/neo4j" stop >/dev/null 2>&1
  [ "$remove" = yes ] && rm -rf "$dir"
}

cleanup() { [ -n "$MINE" ] && stop_instance "$MINE" yes; }
trap cleanup EXIT
trap 'exit 143' INT TERM

# Load the published snapshot into a new private Neo4j and switch the API to it.
switch_to_published() {
  local new creds uri pw body
  new="$(kg_new_dir "${SLURM_JOB_ID:-local}-refresh")" || { log "cannot create an instance dir"; return 1; }
  # A subshell: kg_die inside must not end this loop.
  if ! ( kg_instance_init "$new" >>"$LOG" 2>&1 && kg_instance_load "$SNAP" >>"$LOG" 2>&1 \
         && kg_instance_start >>"$LOG" 2>&1 && printf '%s\n%s\n' "$NEO4J_URI" "$NEO4J_PASSWORD" > "$new/.creds" ); then
    log "could not start the new snapshot; still serving $LOADED"
    stop_instance "$new" yes
    return 1
  fi
  chmod 600 "$new/.creds"
  { read -r uri; read -r pw; } < "$new/.creds"; rm -f "$new/.creds"
  body="$("$RC_KG_PY" -c "import json,sys; print(json.dumps({'uri':sys.argv[1],'user':'neo4j','password':sys.argv[2],'manifest':sys.argv[3]}))" "$uri" "$pw" "$SNAP/manifest.json")"
  if curl -sf --noproxy '*' -X POST -H "Content-Type: application/json" \
       ${RC_KG_API_TOKEN:+-H "Authorization: Bearer $RC_KG_API_TOKEN"} \
       -d "$body" "http://127.0.0.1:$PORT/admin/swap" >>"$LOG" 2>&1; then
    echo >> "$LOG"
    # Stop what was serving: the session's first Neo4j (serve_kg.sh removes its dir
    # at the end), or the one an earlier switch started.
    if [ -n "$MINE" ]; then stop_instance "$MINE" yes; else stop_instance "$SERVING_DIR" no; fi
    MINE="$new"
    LOADED="$(built_at "$SNAP/manifest.json")"
    log "now serving the snapshot built $LOADED"
    return 0
  fi
  log "the API did not switch (see above); still serving $LOADED"
  stop_instance "$new" yes
  return 1
}

# Rebuild from the live sites, if nobody else is; returns 0 if a snapshot was published.
build_if_free() {
  local held now
  now=$(date +%s)
  if ! mkdir "$LOCK" 2>/dev/null; then
    held="$(cat "$LOCK/owner" 2>/dev/null)"
    # A lock whose build ran on this node and whose process is gone was left by a
    # killed build: free at once. One from another node is trusted for LOCK_STALE_S.
    local lhost lpid; read -r lhost _ lpid _ <<<"$held"
    if [ "$lhost" = "$(hostname -s)" ] && [ -n "$lpid" ] && ! kill -0 "$lpid" 2>/dev/null; then
      log "removing the lock of a build that is no longer running ($held)"; rm -rf "$LOCK"
      mkdir "$LOCK" 2>/dev/null || return 1
    elif [ $(( now - $(stat -c %Y "$LOCK" 2>/dev/null || echo "$now") )) -lt "$LOCK_STALE_S" ]; then
      log "another build is running ($held); will switch when it publishes"
      return 1
    else
      log "removing a stale build lock ($held)"; rm -rf "$LOCK"; mkdir "$LOCK" 2>/dev/null || return 1
    fi
  fi
  echo "$(hostname -s) pid $$ user ${USER:-?} job ${SLURM_JOB_ID:-none} since $(date '+%F %T')" > "$LOCK/owner"
  log "snapshot is stale: rebuilding the $VARIANT graph from the live sites (log: $LOG.build)"
  nice -n 10 "$RC_KG_ROOT/scripts/build_graph.sh" --variant "$VARIANT" > "$LOG.build" 2>&1
  local rc=$?
  rm -rf "$LOCK"
  if [ $rc = 0 ]; then log "rebuild published"; else log "rebuild did not publish (exit $rc; the eval gate or a crawl error -- see $LOG.build)"; fi
  return $rc
}

log "started: serving the $VARIANT snapshot built ${LOADED:-?}; checks every $((CHECK_S / 60)) min; rebuild after ${MAX_AGE_H} h ($([ "${RC_KG_REFRESH:-on}" = off ] && echo off || echo on))"
sleep "$FIRST_S"
while kill -0 "$PARENT" 2>/dev/null; do
  pub="$(built_at "$SNAP/manifest.json")"
  if [ -n "$pub" ] && [[ "$pub" > "$LOADED" ]]; then
    log "a newer snapshot is published ($pub)"
    switch_to_published
  elif [ "${RC_KG_REFRESH:-on}" != off ] && [ -n "$pub" ] && [ "$(age_h "$pub")" -ge "$MAX_AGE_H" ]; then
    build_if_free && switch_to_published
  fi
  sleep "$CHECK_S" & wait $!
done
log "session ended"
