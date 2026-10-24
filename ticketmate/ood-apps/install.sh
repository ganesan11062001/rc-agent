#!/usr/bin/env bash
# Install RC Copilot into your Open OnDemand sandbox -- one symlink, no copy.
#
#   /projects/rc/projects/ticketmate/rc-copilot/install.sh             install
#   /projects/rc/projects/ticketmate/rc-copilot/install.sh --uninstall remove the link
#
# ~/ondemand/dev/rc-copilot becomes a symlink to the shared release, so you
# always launch the current version and there is nothing in /scratch to be
# purged. An existing ~/ondemand/dev/rc-copilot is moved aside, never deleted.

set -euo pipefail

# publish.sh copies this script into the releases directory; that is SHARED.
SHARED="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="$SHARED/current/rc-copilot"
DEV="$HOME/ondemand/dev"
LINK="$DEV/rc-copilot"

case "${1:-}" in
  --uninstall)
    if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$TARGET" ]; then
      rm "$LINK"; echo "Removed $LINK."
    else
      echo "$LINK is not a link to the shared RC Copilot; left as it is."
    fi
    exit 0 ;;
  "") ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac

[ -f "$TARGET/manifest.yml" ] || { echo "The shared app is missing at $TARGET; ask the RC Copilot maintainer." >&2; exit 1; }

mkdir -p "$DEV"
if [ -L "$LINK" ] && [ "$(readlink "$LINK")" = "$TARGET" ]; then
  echo "RC Copilot is already installed ($LINK -> $TARGET)."
else
  if [ -e "$LINK" ] || [ -L "$LINK" ]; then
    backup="$LINK.backup-$(date +%Y%m%d-%H%M%S)"
    mv "$LINK" "$backup"
    echo "Moved your existing $LINK to $backup"
  fi
  ln -s "$TARGET" "$LINK"
  echo "Installed: $LINK -> $TARGET"
fi

cat <<EOF

Version: $(head -1 "$SHARED/current/VERSION")

Next:
  1. In Open OnDemand open  Develop -> My Sandbox Apps (Development)  and launch
     RC Copilot. (No "Develop" menu? Ask the OOD admins to enable app
     development for your account.)
  2. Browser extension, once: unzip $SHARED/rc-copilot-extension.zip, then in
     Chrome open chrome://extensions, turn on Developer mode, Load unpacked,
     and pick the unzipped folder.
  3. Open "My Interactive Sessions" once the session is running; the
     extension configures itself from the session card.

Your drafts are logged privately under /scratch/$USER/rc_copilot_runs.
EOF
