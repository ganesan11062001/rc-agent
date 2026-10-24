"""Rule 2: the model never writes a URL.

The model cites retrieved sections as [S1], [S2]. `render` turns those markers
into the section's exact URL and then checks every remaining URL in the text
against what retrieval actually returned. A URL that is not in that set is
removed -- an invented but plausible RC link is worse than no link, because the
person reading the draft will trust it.

One relaxation: if the model writes a URL whose page *was* retrieved but with a
fragment it made up, the URL is cut back to the page rather than dropped.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List

# The leading whitespace is captured so that a marker which expands to nothing
# (an unknown ref) does not leave "word ." behind.
_MARKER = re.compile(r"(\s*)\[(S\d+(?:\s*,\s*S\d+)*)\]")
# Stops at whitespace and at characters that end a URL in prose or markdown.
_URL = re.compile(r"https?://[^\s<>()\[\]\"'`]+")
_TRAILING = ".,;:!?*_"

STYLES = ("markdown", "plain", "numbered")


@dataclass
class Rendered:
    texts: List[str]
    cited: List[str] = field(default_factory=list)          # refs, first-use order
    # numbered style only: [{"n": 1, "ref": "S3", "url": ...}] in n order
    references: List[Dict] = field(default_factory=list)
    removed_urls: List[str] = field(default_factory=list)
    unknown_refs: List[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.texts[0] if self.texts else ""


def _norm(url: str) -> str:
    return url.rstrip("/").lower()


def _page_of(url: str) -> str:
    return _norm(url.split("#", 1)[0])


def render_many(texts: List[str], refs: Dict[str, str], allowed_urls: List[str],
                style: str = "markdown") -> Rendered:
    """Render several texts that belong to one answer -- e.g. a draft reply and
    its suggested steps -- so numbered citations share one numbering.

    `refs` maps "S1" -> url_with_anchor. Styles:
      markdown  [S1](url)
      plain     [S1] <url>           work notes and other plain-text targets
      numbered  [1]  + `references`  the caller prints the list once, e.g. as
                                     a References block under an email reply
    """
    if style not in STYLES:
        raise ValueError("style must be one of %s" % (STYLES,))
    out = Rendered(texts=[])
    allowed = {_norm(u): u for u in allowed_urls}
    allowed_pages = {_page_of(u): u.split("#", 1)[0] for u in allowed_urls}
    number: Dict[str, int] = {}

    # 1. Validate the URLs the model wrote itself, before markers add ours.
    def fix_url(m: "re.Match") -> str:
        raw = m.group(0)
        url = raw.rstrip(_TRAILING)
        tail = raw[len(url):]
        if _norm(url) in allowed:
            return allowed[_norm(url)] + tail
        if _page_of(url) in allowed_pages:
            return allowed_pages[_page_of(url)] + tail
        out.removed_urls.append(url)
        return "[link removed]" + tail

    # 2. Expand citation markers.
    def expand(m: "re.Match") -> str:
        parts = []
        for ref in re.split(r"\s*,\s*", m.group(2)):
            url = refs.get(ref)
            if not url:
                out.unknown_refs.append(ref)
                continue
            if ref not in out.cited:
                out.cited.append(ref)
            if style == "markdown":
                parts.append("[%s](%s)" % (ref, url))
            elif style == "plain":
                parts.append("[%s] <%s>" % (ref, url))
            else:
                if ref not in number:
                    number[ref] = len(number) + 1
                    out.references.append({"n": number[ref], "ref": ref, "url": url})
                parts.append("[%d]" % number[ref])
        return m.group(1) + " ".join(parts) if parts else ""

    for text in texts:
        out.texts.append(_MARKER.sub(expand, _URL.sub(fix_url, text or "")))
    return out


def render(text: str, refs: Dict[str, str], allowed_urls: List[str],
           style: str = "markdown") -> Rendered:
    return render_many([text], refs, allowed_urls, style)
