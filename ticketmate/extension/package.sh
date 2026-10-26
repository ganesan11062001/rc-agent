#!/usr/bin/env bash
# Build dist/rc-copilot-extension.zip from this directory, after the static
# checks pass.
#
#   extension/package.sh            check, then zip the version in manifest.json
#
# Bump "version" in manifest.json first: Chrome shows it on chrome://extensions,
# and it is how a member can tell they reloaded the new build. The zip holds
# exactly what the browser loads (manifest, scripts, styles, popup, icons) at
# its top level, so "Load unpacked" on the unzipped folder works. Publishing it
# to RC members is ood-apps/publish.sh, which copies this zip.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TM_ROOT="$(cd "$HERE/.." && pwd)"
OUT="$TM_ROOT/dist/rc-copilot-extension.zip"
PY="$TM_ROOT/backend/.venv/bin/python"      # check.py needs esprima, which is in this venv

FILES=(manifest.json background.js content.js content.css ood-connect.js
       popup.html popup.js popup.css icons/icon-16.png icons/icon-32.png
       icons/icon-48.png icons/icon-128.png)

"$PY" "$HERE/check.py" || { echo "package: static checks failed; nothing written" >&2; exit 1; }

mkdir -p "$(dirname "$OUT")"
# Build beside the target and rename, so a failed run never leaves a half zip.
"$PY" - "$HERE" "$OUT.tmp" "${FILES[@]}" <<'PY'
import json, os, sys, zipfile
src, out, files = sys.argv[1], sys.argv[2], sys.argv[3:]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    for f in files:
        z.write(os.path.join(src, f), f)
version = json.load(open(os.path.join(src, "manifest.json")))["version"]
print("packaged RC Copilot %s: %d files" % (version, len(files)))
PY
mv -f "$OUT.tmp" "$OUT"
echo "wrote $OUT"
