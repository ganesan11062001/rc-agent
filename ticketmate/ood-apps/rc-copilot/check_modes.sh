#!/usr/bin/env bash
# OOD copies template/ file modes verbatim when staging a session. A template
# script.sh.erb at 644 stages as a non-executable script.sh, and the job dies
# with "Permission denied" before anything of ours runs.
#
# Easy to lose: git doesn't track modes beyond the exec bit, `cat > file`
# creates 644, and most copy tools drop it.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
fail=0
for f in rc-copilot/template/*.erb capture-test/template/*.erb; do
  m=$(stat -c '%a' "$f")
  if [ "$m" = "755" ]; then printf '  OK    %s (%s)\n' "$f" "$m"
  else printf '  FIX   %s is %s, must be 755\n' "$f" "$m"; fail=1; fi
done
[ "$fail" = 1 ] && { echo; echo "Run: chmod 755 rc-copilot/template/*.erb capture-test/template/*.erb"; exit 1; }
echo; echo "All template scripts executable."
