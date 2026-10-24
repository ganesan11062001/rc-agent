#!/usr/bin/env bash
# List your rc-kg instances on this node, and optionally clean up stale ones.
#
#   scripts/kg_ps.sh           live / STALE, size, processes
#   scripts/kg_ps.sh --clean   stop and remove the STALE ones (live ones are never touched)
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/_kg_instance.sh"
case "${1:-}" in
  --clean) kg_sweep_stale; kg_list ;;
  ""|--list) kg_list ;;
  *) sed -n '2,6p' "$0"; exit 1 ;;
esac
