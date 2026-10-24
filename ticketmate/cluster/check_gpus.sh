#!/usr/bin/env bash
# What GPUs can RC Copilot actually get right now?
#
#   ./check_gpus.sh          summary
#   ./check_gpus.sh -v       plus per-node detail
#
# Run this on a LOGIN NODE (needs the Slurm client tools).
#
# Two questions it answers:
#   1. Does the feature string "a100@80g" really exist? The OOD app passes it as
#      --constraint for the GLM option. If it's wrong, that job never schedules
#      and just sits Pending with "Requested node configuration is not available".
#   2. Is anything free now, so you know whether to use the 3B test model.

set -uo pipefail
VERBOSE=0
[ "${1:-}" = "-v" ] && VERBOSE=1

command -v sinfo >/dev/null 2>&1 || {
  echo "error: sinfo not found. Run this on a login node (e.g. explorer-02)." >&2
  exit 1
}

GPU_PARTS="gpu-interactive,gpu-short,gpu,multigpu"

echo "════════════════════════════════════════════════════════════════"
echo " 1. GPU types on the cluster (Gres per node)"
echo "════════════════════════════════════════════════════════════════"
sinfo -h -o '%G' -p "$GPU_PARTS" 2>/dev/null \
  | tr ',' '\n' | grep -i gpu | sed 's/(.*)//' | sort | uniq -c | sort -rn \
  | sed 's/^/  /'

echo
echo "════════════════════════════════════════════════════════════════"
echo " 2. Feature strings  <-- these are what --constraint matches"
echo "════════════════════════════════════════════════════════════════"
sinfo -h -o '%f' -p "$GPU_PARTS" 2>/dev/null \
  | tr ',' '\n' | sed 's/^ *//; s/ *$//' | grep -v '^$' | grep -v '^(null)$' \
  | sort -u | sed 's/^/  /'

echo
echo "  Does 'a100@80g' exist?  (the OOD app's GLM option depends on it)"
if sinfo -h -o '%f' -p "$GPU_PARTS" 2>/dev/null | tr ',' '\n' | grep -qx 'a100@80g'; then
  echo "    YES -- the GLM-4.7-Flash option will schedule."
else
  echo "    NO  -- nothing advertises exactly 'a100@80g'."
  echo "    Closest matches:"
  sinfo -h -o '%f' -p "$GPU_PARTS" 2>/dev/null | tr ',' '\n' \
    | grep -i 'a100\|80' | sort -u | sed 's/^/      /'
  echo "    -> put the right string in ood-apps/rc-copilot/submit.yml.erb (constraint = ...)"
fi

echo
echo "════════════════════════════════════════════════════════════════"
echo " 3. What is free right now"
echo "════════════════════════════════════════════════════════════════"
printf "  %-14s %-16s %-26s %s\n" STATE PARTITION GRES NODES
sinfo -h -N -p "$GPU_PARTS" -o '%t|%P|%G|%N' 2>/dev/null \
  | awk -F'|' '$1 ~ /idle|mix/ {print}' \
  | sort -t'|' -k3 \
  | awk -F'|' '{printf "  %-14s %-16s %-26s %s\n", $1, $2, $3, $4}' \
  | head -30
echo
echo "  (idle = completely free, mix = partly allocated but may have free GPUs)"

echo
echo "════════════════════════════════════════════════════════════════"
echo " 4. Verdict for each model option"
echo "════════════════════════════════════════════════════════════════"

free_any=$(sinfo -h -N -p "$GPU_PARTS" -o '%t|%G' 2>/dev/null \
           | awk -F'|' '$1 ~ /idle|mix/ && $2 ~ /gpu/' | wc -l)
free_80=$(sinfo -h -N -p "$GPU_PARTS" -o '%t|%f|%G' 2>/dev/null \
          | awk -F'|' '$1 ~ /idle|mix/ && ($2 ~ /80g/ || $3 ~ /80/)' | wc -l)

printf "  %-28s %s\n" "Qwen2.5-3B (test, gpu:1)" \
  "$([ "$free_any" -gt 0 ] && echo "GO -- $free_any node(s) with a free-ish GPU" || echo "wait -- nothing idle")"
printf "  %-28s %s\n" "GLM-4.7-Flash (a100 80GB)" \
  "$([ "$free_80" -gt 0 ] && echo "GO -- $free_80 candidate node(s)" || echo "wait -- no 80GB A100 idle")"

echo
echo "  Your current jobs:"
squeue -u "$USER" -h -o '    %i %P %T %L %R' 2>/dev/null | sed 's/^/  /' \
  || echo "    (none)"

if [ "$VERBOSE" = 1 ]; then
  echo
  echo "════════════════════════════════════════════════════════════════"
  echo " 5. Per-node detail"
  echo "════════════════════════════════════════════════════════════════"
  for n in $(sinfo -h -N -p "$GPU_PARTS" -o '%N' 2>/dev/null | sort -u | head -12); do
    scontrol show node "$n" 2>/dev/null \
      | grep -oE 'NodeName=[^ ]+|Gres=[^ ]+|AvailableFeatures=[^ ]+|State=[^ ]+|AllocTRES=[^ ]*' \
      | paste -sd' ' | sed 's/^/  /'
  done
fi
