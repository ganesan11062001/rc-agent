#!/usr/bin/env python3
"""Static checks for the extension, run before packaging.

Every check here exists because something it catches actually shipped broken:

* duplicate const/let  -- a duplicate PANEL_ID made the whole content script
  throw SyntaxError at load, so nothing rendered at all. esprima's
  parseScript() does NOT do scope analysis, so a plain parse passes.
* orphaned var(--rc-*) -- the CSS token block named a deleted element id, so
  the collapsed button had no background and was invisible.
* stale selectors     -- JS querying a class the markup no longer emits fails
  silently.

Usage:  ../backend/.venv/bin/python check.py     (exit 1 on any failure)

Needs `esprima`, which is already in the backend venv -- hence the interpreter
above. A bare `python3 check.py` will not find it.
"""

import collections
import glob
import json
import os
import re
import sys

try:
    import esprima
except ImportError:
    sys.exit(
        "check.py needs the 'esprima' module.\n"
        "Run it with the backend venv, which already has it:\n"
        "    ../backend/.venv/bin/python check.py\n"
        "or install it:  pip install esprima"
    )

EXT = os.path.dirname(os.path.abspath(__file__))
fails = []


def ok(label, detail=""):
    print("  \033[32mPASS\033[0m  %-44s %s" % (label, detail))


def bad(label, detail=""):
    print("  \033[31mFAIL\033[0m  %-44s %s" % (label, detail))
    fails.append(label)


def read(name):
    with open(os.path.join(EXT, name), encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------- 1. parse
sources = {}
for path in sorted(glob.glob(os.path.join(EXT, "*.js"))):
    name = os.path.basename(path)
    src = read(name)
    sources[name] = src
    try:
        esprima.parseScript(src)
        ok("parses", name)
    except Exception as exc:
        bad("parses", "%s -> %s" % (name, exc))


# ------------------------------------------- 2. duplicate lexical bindings
def scopes(node, out=None):
    """Yield every function/program body as a list of statements."""
    if out is None:
        out = []
    body = getattr(node, "body", None)
    if isinstance(body, list):
        out.append(body)
    elif body is not None and getattr(body, "type", "") == "BlockStatement":
        out.append(body.body)
    for key in dir(node):
        if key.startswith("_"):
            continue
        child = getattr(node, key, None)
        if hasattr(child, "type"):
            scopes(child, out)
        elif isinstance(child, list):
            for item in child:
                if hasattr(item, "type"):
                    scopes(item, out)
    return out


for name, src in sources.items():
    tree = esprima.parseScript(src)
    dupes = {}
    for body in scopes(tree):
        counts = collections.Counter()
        for stmt in body:
            if stmt.type == "VariableDeclaration" and stmt.kind in ("const", "let"):
                for decl in stmt.declarations:
                    if getattr(decl.id, "name", None):
                        counts[decl.id.name] += 1
        dupes.update({k: v for k, v in counts.items() if v > 1})
    if dupes:
        bad("no duplicate const/let", "%s -> %s" % (name, dupes))
    else:
        ok("no duplicate const/let", name)


# ------------------------------------------------- 3. CSS tokens reach roots
css_raw = read("content.css")
css = re.sub(r"/\*.*?\*/", "", css_raw, flags=re.S)
js = sources["content.js"]

declared = set()
for m in re.finditer(r"([^{}]+)\{([^}]*--rc-accent\s*:)", css):
    for sel in m.group(1).split(","):
        sel = sel.strip()
        if sel.startswith("#"):
            declared.add(sel[1:])

created = set(re.findall(r'(?:FAB_ID|PANEL_ID)\s*=\s*"([\w-]+)"', js))
missing = sorted(created - declared)
if missing:
    bad("CSS tokens reach every root", "no tokens for %s" % missing)
else:
    ok("CSS tokens reach every root", ", ".join(sorted(created)))

orphans = [
    m.group(1).strip()
    for m in re.finditer(r"^((?:button|aside|div)#([\w-]+)[^{]*)\{([^}]*)\}", css, re.M)
    if "var(--rc-" in m.group(3) and m.group(2) not in declared
]
if orphans:
    bad("no var() outside a token root", str(orphans))
else:
    ok("no var() outside a token root")


# ---------------------------------------------------- 4. selectors vs markup
emitted = {c for g in re.findall(r'class="((?:rc-[\w-]+\s*)+)"', js) for c in g.split()}
emitted |= set(re.findall(r'className = "([\w-]+)"', js))

queried = set(re.findall(r'\$\("\.([\w-]+)"\)', js))
queried |= set(re.findall(r'querySelector\("\.([\w-]+)"\)', js))
gap = sorted(queried - emitted)
if gap:
    bad("every queried class is emitted", str(gap))
else:
    ok("every queried class is emitted", "%d queried" % len(queried))

HOOKS = {"rc-copy", "rc-insert", "rc-go", "rc-collapse",
         "rc-sec-ticket", "rc-sec-compose", "rc-sec-result"}
unstyled = sorted(c for c in emitted - HOOKS if "." + c not in css)
if unstyled:
    bad("every emitted class is styled", str(unstyled))
else:
    ok("every emitted class is styled", "%d classes" % len(emitted))


# --------------------------------------------------------- 5. message wiring
handled = set(re.findall(r"^\s{4}([A-Z_]+):\s*\(\)", sources["background.js"], re.M))
sent = set()
for name in ("content.js", "popup.js", "ood-connect.js"):
    sent |= set(re.findall(r'type:\s*"([A-Z_]+)"', sources[name]))
unhandled = sorted(sent - handled)
if unhandled:
    bad("every message type is handled", str(unhandled))
else:
    ok("every message type is handled", ", ".join(sorted(sent)))


# ------------------------------------------------------------- 6. manifest
manifest = json.loads(read("manifest.json"))
refs = [manifest["background"]["service_worker"], manifest["action"]["default_popup"]]
for entry in manifest["content_scripts"]:
    refs += entry["js"] + entry.get("css", [])
refs += list(manifest["icons"].values())
refs += list(manifest["action"]["default_icon"].values())
refs += ["popup.js", "popup.css"]
absent = sorted({r for r in refs if not os.path.exists(os.path.join(EXT, r))})
if absent:
    bad("manifest references exist", str(absent))
else:
    ok("manifest references exist", "v%s" % manifest["version"])

popup_ids = set(re.findall(r'id="([^"]+)"', read("popup.html")))
popup_used = set(re.findall(r'el\("([^"]+)"\)', sources["popup.js"]))
popup_gap = sorted(popup_used - popup_ids)
if popup_gap:
    bad("popup element ids resolve", str(popup_gap))
else:
    ok("popup element ids resolve", "%d ids" % len(popup_used))


print()
if fails:
    print("  %d check(s) failed" % len(fails))
    sys.exit(1)
print("  all checks passed")
