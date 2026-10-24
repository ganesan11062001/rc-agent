#!/usr/bin/env bash
# Pack the venv's site-packages into one archive that serving unpacks to
# node-local disk, instead of importing 37,000 files over NFS.
#
#   scripts/build_runtime.sh          (re)build if the installed packages changed
#   scripts/build_runtime.sh --force
#
# Why: importing sentence_transformers from /projects spends ~70 s in ~55,000
# stat/open calls at ~1.3 ms each (measured 2026-10-22, strace -w); CPU time is
# ~8 s, and the delay grows when NFS is busy. From local disk the same import
# takes ~10 s, and unpacking 1.3 GB takes ~7 s once per node.
#
# Output: vendor/runtime/site-packages.tar + .stamp (a fingerprint of the
# installed distributions). serve_kg.sh only uses the archive while the stamp
# still matches the venv, so a `pip install` can never serve stale packages.
# Packing reads the venv over NFS and takes a few minutes; it is only needed
# after the dependencies change. build_graph.sh runs it when they have.

set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"

OUT="$RC_KG_ROOT/vendor/runtime"
want="$(kg_runtime_stamp)" || kg_die "cannot fingerprint $KG_SITE"
if [ "${1:-}" != "--force" ] && [ -f "$OUT/site-packages.tar" ] \
   && [ "$(cat "$OUT/site-packages.stamp" 2>/dev/null)" = "$want" ]; then
  kg_log "runtime archive is current ($want)"
  exit 0
fi

mkdir -p "$OUT" || kg_die "cannot create $OUT"
tmp="$OUT/.site-packages.tar.$$"
trap 'rm -f "$tmp"' EXIT
kg_log "packing $KG_SITE (a few minutes over NFS)"
tar -C "$KG_SITE" --exclude='__pycache__/*.pyc.*' -cf "$tmp" . || kg_die "tar failed"
chmod a+r "$tmp"
# Archive first, stamp second: a reader that sees the new stamp also sees the
# new archive. Each rename is atomic.
mv -f "$tmp" "$OUT/site-packages.tar"
echo "$want" > "$OUT/site-packages.stamp.$$" && chmod a+r "$OUT/site-packages.stamp.$$" \
  && mv -f "$OUT/site-packages.stamp.$$" "$OUT/site-packages.stamp"
kg_log "wrote $OUT/site-packages.tar ($(du -h "$OUT/site-packages.tar" | cut -f1), stamp $want)"
