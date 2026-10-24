#!/usr/bin/env bash
# Publish the RC Copilot OOD app for every RC member.
#
#   ood-apps/publish.sh            publish the app as it is in this checkout
#   ood-apps/publish.sh --list     show releases and which one is live
#   ood-apps/publish.sh --rollback switch back to the previous release
#
# Members install once with <monorepo>/rc-copilot/install.sh,
# which symlinks ~/ondemand/dev/rc-copilot to the live release. Publishing
# therefore changes everyone's app at once, so it is done in two steps that
# cannot leave anyone with a half-copied app:
#
#   1. copy into a new  releases/<date>-<commit>/  directory
#   2. rename a new `current` symlink over the old one (rename is atomic)
#
# Sessions already running are unaffected: OOD copies the template into each
# session's own staging directory at launch. The last 3 releases are kept.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TM_ROOT="$(cd "$HERE/.." && pwd)"
# The monorepo root; releases go in its rc-copilot/ (gitignored).
TICKETMATE_HOME="$(cd "$TM_ROOT/.." && pwd)"
DEST="${RC_COPILOT_RELEASES:-$TICKETMATE_HOME/rc-copilot}"
KEEP=3

die() { echo "publish: $*" >&2; exit 1; }

list() {
  local live; live="$(readlink "$DEST/current" 2>/dev/null || true)"
  for r in "$DEST"/releases/*/; do
    r="${r%/}"; [ -d "$r" ] || continue
    printf '%s %s  %s\n' "$([ "releases/$(basename "$r")" = "$live" ] && echo '*' || echo ' ')" \
      "$(basename "$r")" "$(head -1 "$r/VERSION" 2>/dev/null)"
  done
}

swap_to() {            # atomically point `current` at releases/$1
  ln -s "releases/$1" "$DEST/.current.$$"
  mv -T "$DEST/.current.$$" "$DEST/current"
}

case "${1:-}" in
  --list) list; exit 0 ;;
  --rollback)
    live="$(basename "$(readlink "$DEST/current")")"
    prev="$(ls -1 "$DEST/releases" | grep -v "^$live$" | sort | tail -1)"
    [ -n "$prev" ] || die "no earlier release to roll back to"
    swap_to "$prev"; echo "live release is now $prev"; list; exit 0 ;;
  "") ;;
  *) sed -n '2,18p' "$0"; exit 1 ;;
esac

[ -f "$HERE/rc-copilot/manifest.yml" ] || die "no app at $HERE/rc-copilot"
commit="$(git -C "$TM_ROOT" rev-parse --short HEAD)"
dirty=""
if [ -n "$(git -C "$TM_ROOT" status --porcelain -- ood-apps/rc-copilot dist)" ]; then
  dirty="-dirty"
  echo "publish: note: ood-apps/rc-copilot or dist/ has uncommitted changes; they are published too"
fi
stamp="$(date +%Y%m%d-%H%M%S)-$commit$dirty"
rel="$DEST/releases/$stamp"

umask 022
mkdir -p "$DEST/releases"
mkdir "$rel"
cp -r "$HERE/rc-copilot" "$rel/rc-copilot"
# A repo-level developer check (it inspects sibling apps); misleading here.
rm -f "$rel/rc-copilot/check_modes.sh"
# The one place the install location is recorded; the session script reads it.
printf 'TICKETMATE_HOME=%q\n' "$TICKETMATE_HOME" > "$rel/rc-copilot/template/ticketmate.env"
cp "$TM_ROOT/dist/rc-copilot-extension.zip" "$rel/"
cat > "$rel/VERSION" <<EOF
RC Copilot $stamp, published $(date -Iseconds) by $USER
ticketmate commit: $(git -C "$TM_ROOT" log -1 --format='%h %s')
EOF
chmod -R a+rX "$rel"

# The member-facing files live beside the releases, refreshed every publish.
for f in install.sh MEMBERS.md; do
  cp "$HERE/$f" "$DEST/.$f.$$" && chmod a+r "$DEST/.$f.$$" && mv -f "$DEST/.$f.$$" "$DEST/${f/MEMBERS/README}"
done
chmod a+rx "$DEST/install.sh"
ln -sfn current/rc-copilot-extension.zip "$DEST/rc-copilot-extension.zip"

swap_to "$stamp"
chmod a+rX "$DEST" "$DEST/releases"

# Keep the newest $KEEP releases (the live one is always among them).
ls -1 "$DEST/releases" | sort | head -n -"$KEEP" | while read -r old; do
  [ "releases/$old" = "$(readlink "$DEST/current")" ] || rm -rf "${DEST:?}/releases/$old"
done

echo "published $stamp -> $DEST/current"
list
