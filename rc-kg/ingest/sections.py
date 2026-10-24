"""Split `content.rendered` into Sections on H2/H3 boundaries.

Rules, straight from the plan:
  * a new Section starts at every H2 or H3
  * content before the first heading becomes an "Overview" section
  * H4-H6 stay inside whichever section they fall in

HOW, AND WHY IT IS DONE THIS WAY

Block themes wrap content in nested <div class="wp-block-group">, so headings
are rarely children of the body -- a naive loop over `body.children` collapses
most pages to a single section. The obvious fix is to recurse into wrappers,
but recursion plus "append the rest to the current bucket" double-counts:
an ancestor's text already contains its descendants', so any node reachable by
two paths is emitted twice. A first version of this file did exactly that and
stored 316,877 characters for a 206,740-character corpus, with one section of
108,656 characters.

So the document is instead flattened ONCE into a linear stream of
(kind, node) events in document order, where a node is emitted only if it has
no H2/H3 inside it. Every piece of content therefore appears in exactly one
event, and the splitter is a simple loop over that stream.

Text extraction is not a plain `get_text()`: this corpus has 12 tables, 198
code blocks and 308 list items, and flattening those into one space-joined
line destroys the structure that makes an answer usable.
"""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Iterator, List, Tuple

from bs4 import BeautifulSoup, NavigableString, Tag

SECTION_HEADINGS = ("h2", "h3")
OVERVIEW_HEADING = "Overview"

# Elements whose text we render specially; anything else falls back to get_text.
_STRUCTURED = ("ul", "ol", "table", "pre", "h4", "h5", "h6")

_WS = re.compile(r"[ \t ]+")
_BLANKS = re.compile(r"\n{3,}")


@dataclass
class ParsedSection:
    """One Section before it is given a URL or an embedding."""

    heading: str
    heading_level: int          # 2 or 3; 0 for the synthetic Overview
    text: str
    order: int                  # reading order within the page, 0-based

    hrefs: List[str] = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        """Hash of heading + text, so re-embedding is skipped when unchanged.

        Deliberately excludes the URL and the anchor: a theme change that moves
        an anchor is not a content change and must not force a re-embed.
        """
        payload = "%s\x00%s" % (self.heading, self.text)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def heading_text(node: Tag) -> str:
    return _WS.sub(" ", node.get_text(" ", strip=True)).strip()


def _has_section_heading(node: Tag) -> bool:
    return node.find(SECTION_HEADINGS) is not None


def _flatten(root: Tag) -> Iterator[Tuple[str, object]]:
    """Yield ("heading", tag) and ("content", node) in document order.

    Invariant: a node yielded as "content" is never an ancestor or descendant
    of another yielded node, so no text is emitted twice.
    """
    for child in root.children:
        if isinstance(child, NavigableString):
            if child.strip():
                yield "content", child
            continue
        if not isinstance(child, Tag):
            continue

        if child.name in SECTION_HEADINGS:
            yield "heading", child
        elif _has_section_heading(child):
            # Must descend: cutting here would hide the headings inside.
            yield from _flatten(child)
        else:
            # Self-contained block. Emit whole and do NOT descend.
            yield "content", child


def _block_text(node: object) -> str:
    """Readable plain text for one emitted node, preserving its shape."""
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag):
        return ""

    name = node.name or ""

    if name == "pre":
        # Commands have to survive intact and on their own lines.
        return "\n" + node.get_text("\n", strip=True) + "\n"

    if name in ("ul", "ol"):
        lines = []
        for i, li in enumerate(node.find_all("li", recursive=False), start=1):
            bullet = "%d." % i if name == "ol" else "-"
            lines.append("%s %s" % (bullet, li.get_text(" ", strip=True)))
        return "\n".join(lines)

    if name == "table":
        lines = []
        for tr in node.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            if any(cells):
                lines.append(" | ".join(cells))
        return "\n".join(lines)

    if name in ("h4", "h5", "h6"):
        # Kept inline and on its own line, so sub-structure is visible.
        return "\n%s\n" % node.get_text(" ", strip=True)

    # A generic container may still hold lists/tables/pre worth shaping.
    if node.find(_STRUCTURED):
        parts = []
        for kid in node.children:
            if isinstance(kid, NavigableString):
                if kid.strip():
                    parts.append(str(kid).strip())
            elif isinstance(kid, Tag):
                parts.append(_block_text(kid))
        return "\n".join(p for p in parts if p)

    return node.get_text(" ", strip=True)


def _clean(text: str) -> str:
    text = _WS.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _BLANKS.sub("\n\n", text).strip()


def _hrefs(nodes: List[object]) -> List[str]:
    out: List[str] = []
    for n in nodes:
        if not isinstance(n, Tag):
            continue
        anchors = [n] if n.name == "a" else n.find_all("a")
        for a in anchors:
            href = a.get("href")
            if href and href.strip():
                out.append(href.strip())
    return out


def split_sections(content_html: str) -> List[ParsedSection]:
    """Split one page body into sections.

    Sections with no body text are dropped: a heading with nothing under it
    cannot support an answer, and keeping it only pollutes retrieval. A page
    whose body is entirely non-textual (a bare shortcode or embed) therefore
    legitimately yields zero sections, which the caller reports.
    """
    soup = BeautifulSoup(content_html or "", "lxml")

    # BeautifulSoup's get_text() includes <script> and <style> bodies. The GPU
    # dashboard plugin on /getting-help-2/ inlines ~117 KB of JavaScript, which
    # is 120x that page's actual prose -- it would become the largest "section"
    # in the corpus and poison retrieval. Remove non-prose elements first.
    for junk in soup.find_all(["script", "style", "noscript", "template", "svg", "iframe"]):
        junk.decompose()

    root = soup.body or soup

    buckets: List[Tuple[str, int, List[object]]] = [(OVERVIEW_HEADING, 0, [])]
    for kind, node in _flatten(root):
        if kind == "heading":
            buckets.append((heading_text(node), int(node.name[1]), []))
        else:
            buckets[-1][2].append(node)

    sections: List[ParsedSection] = []
    for heading, level, nodes in buckets:
        text = _clean("\n\n".join(_block_text(n) for n in nodes))
        if not text:
            continue
        sections.append(
            ParsedSection(
                heading=heading or OVERVIEW_HEADING,
                heading_level=level,
                text=text,
                order=len(sections),
                hrefs=_hrefs(nodes),
            )
        )
    return sections
