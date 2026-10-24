#!/usr/bin/env bash
#SBATCH --job-name=rc-kg-rebuild
#SBATCH --partition=short
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=01:30:00
#SBATCH --output=/scratch/%u/rc_kg_rebuild/%j.out
#
# Rebuild and publish both knowledge-graph variants from the live sites.
# CPU only. Each variant is published only if its eval gate and graph
# integrity checks pass; otherwise the graph TicketMate sessions load stays
# as it was and this job exits non-zero.
#
# One-off:      sbatch /projects/rc/projects/ticketmate/rc-kg/scripts/rebuild.sbatch
# Weekly (Slurm's per-user cron; `scrontab -e` and add this line):
#   #SCRON --partition=short --time=01:30:00 --cpus-per-task=4 --mem=16G
#   0 3 * * 1 /projects/rc/projects/ticketmate/rc-kg/scripts/rebuild.sbatch
#
# Logs: /scratch/$USER/rc_kg_rebuild/<jobid>.out (create the directory once:
# mkdir -p /scratch/$USER/rc_kg_rebuild).

set -uo pipefail
# sbatch runs a spooled copy of this script, so ask Slurm where the original is.
_self="${BASH_SOURCE[0]}"
if [ -n "${SLURM_JOB_ID:-}" ] && command -v scontrol >/dev/null; then
  _cmd="$(scontrol show job "$SLURM_JOB_ID" 2>/dev/null | sed -n 's/^ *Command=\([^ ]*\).*/\1/p')"
  [ -f "$_cmd" ] && _self="$_cmd"
fi
ROOT="${RC_KG_ROOT:-$(cd "$(dirname "$_self")/.." && pwd)}"
[ -x "$ROOT/scripts/build_graph.sh" ] || { echo "ERROR: cannot locate rc-kg (got $ROOT); set RC_KG_ROOT" >&2; exit 1; }
echo "rc-kg rebuild on $(hostname -s) at $(date -Iseconds)"
"$ROOT/scripts/build_graph.sh" --all
rc=$?
for d in graph graph-website; do
  python3 -c "import json; m=json.load(open('$ROOT/data/$d/manifest.json')); print('%-14s %s built %s commit %s hit@5 %s integrity %s' % ('$d', m['variant'], m['built_at'], m['git_commit'], m['eval']['hit@5'], m.get('graph_integrity')))"
done
exit $rc
