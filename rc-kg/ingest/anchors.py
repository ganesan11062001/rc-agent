"""Resolve a section heading to a linkable fragment on the live page.

Two sources, in order of quality:

  1. A real `id` on the live page's heading element. Best: it is a plain
     fragment, works in every browser and in email.
  2. A Scroll-to-Text Fragment, `#:~:text=<encoded heading>`. Works in
     Chromium and Safari 16.1+, **not in Firefox**, where it degrades to
     landing at the top of the page.

Measured on rc.northeastern.edu (2026-10-05): 26 of 279 headings carry an id in
`content.rendered`, and spot checks of the live HTML found *zero* -- the theme
adds none. So option 2 is the common case here, which is why `anchor_kind` is
stored per Section: the retrieval layer can then prefer id-anchored sections
when two sections are equally relevant, and the report can show how much of
the corpus is only reachable by text fragment.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Optional
from urllib.parse import quote

from bs4 import BeautifulSoup

from .http import session

logger = logging.getLogger(__name__)

ANCHOR_ID = "id"
ANCHOR_TEXT_FRAGMENT = "text_fragment"
ANCHOR_NONE = "none"

# A text fragment longer than this is unwieldy in a pasted link and more likely
# to fail to match after a copy-edit. Chrome matches on a prefix fine.
_MAX_FRAGMENT_CHARS = 60
_NORM = re.compile(r"[^a-z0-9]+")


def _key(text: str) -> str:
    """Loose match key: case, punctuation and spacing all vary between the API
    body and the rendered page (smart quotes, &amp;, trailing colons)."""
    return _NORM.sub(" ", (text or "").lower()).strip()


@dataclass
class Anchor:
    url_with_anchor: str
    kind: str               # ANCHOR_ID | ANCHOR_TEXT_FRAGMENT | ANCHOR_NONE


def fetch_live_heading_ids(page_url: str) -> Dict[str, str]:
    """Map heading-text-key -> id for every id-bearing heading on the live page.

    One request per page. A failure returns {} and is logged: a missing live
    page must degrade to text fragments, not abort the ingest.
    """
    try:
        resp = session().get(page_url)
    except Exception as exc:                      # noqa: BLE001 - never fatal
        logger.warning("anchor scrape failed for %s: %s", page_url, exc)
        return {}
    if resp.status_code != 200:
        logger.warning("anchor scrape got HTTP %d for %s", resp.status_code, page_url)
        return {}

    soup = BeautifulSoup(resp.text, "lxml")
    found: Dict[str, str] = {}
    for tag in soup.find_all(["h2", "h3", "h4"]):
        hid = tag.get("id")
        if not hid:
            continue
        k = _key(tag.get_text(" ", strip=True))
        if k and k not in found:
            found[k] = hid
    return found


def text_fragment(heading: str) -> str:
    """Build the `:~:text=` payload for a heading."""
    snippet = " ".join((heading or "").split())[:_MAX_FRAGMENT_CHARS].strip()
    return quote(snippet, safe="")


def disambiguate(url_with_anchor: str, section_text: str) -> str:
    """Make a text-fragment link unique when its heading repeats on the page.

    A browser jumps to the first match of `#:~:text=Solution%3A`, so the
    second "Solution:" section on a page was linked to the first one. The
    fragment syntax takes a suffix -- `text=Solution%3A,-We%20can` -- that
    must follow the match, and the section's own opening words do.
    """
    words = " ".join((section_text or "").split()[:4])
    return "%s,-%s" % (url_with_anchor, quote(words, safe="")) if words else url_with_anchor


def build_anchor(
    page_url: str,
    heading: str,
    heading_level: int,
    live_ids: Dict[str, str],
    inline_id: Optional[str] = None,
) -> Anchor:
    """Pick the best fragment for one section.

    `inline_id` is an id already present in content.rendered -- trusted over the
    live scrape, because it is authored content rather than theme output.
    """
    base = page_url.rstrip("/")

    # The synthetic Overview section is the top of the page; a fragment would
    # scroll past the very content it names.
    if heading_level == 0:
        return Anchor(base + "/", ANCHOR_NONE)

    if inline_id:
        return Anchor("%s/#%s" % (base, inline_id), ANCHOR_ID)

    hit = live_ids.get(_key(heading))
    if hit:
        return Anchor("%s/#%s" % (base, hit), ANCHOR_ID)

    frag = text_fragment(heading)
    if frag:
        return Anchor("%s/#:~:text=%s" % (base, frag), ANCHOR_TEXT_FRAGMENT)

    return Anchor(base + "/", ANCHOR_NONE)


def inline_heading_ids(content_html: str) -> Dict[str, str]:
    """Heading ids already present in the API body, keyed the same way."""
    soup = BeautifulSoup(content_html or "", "lxml")
    out: Dict[str, str] = {}
    for tag in soup.find_all(["h2", "h3"]):
        hid = tag.get("id")
        if hid:
            k = _key(tag.get_text(" ", strip=True))
            if k and k not in out:
                out[k] = hid
    return out


def summarize(anchors: List[Anchor]) -> Dict[str, int]:
    counts = {ANCHOR_ID: 0, ANCHOR_TEXT_FRAGMENT: 0, ANCHOR_NONE: 0}
    for a in anchors:
        counts[a.kind] = counts.get(a.kind, 0) + 1
    return counts
