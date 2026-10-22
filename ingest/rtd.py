"""Read the Docs source: rc-docs.northeastern.edu (Sphinx, furo theme).

The RC website answers "what and who"; rc-docs answers "how" -- commands,
partitions, modules -- and the website links to it more than to any other
domain (82 links). This module turns it into the same Page/Section rows the
WordPress ingest writes, reusing the sectioner, so both sites are one graph.

Choices, each measured on 2026-10-22:

  Version: `latest`, built from `master`. The sitemap lists two versions;
  `explorer-main` is 17 commits behind (older GPU table and MGHPCC shutdown
  date, no data-transfer guide or storage-query page). Links anywhere to
  /en/<any version>/page.html are mapped to the latest page.

  Page list: Sphinx's searchindex.js (55 docnames), not a crawl -- it is the
  build's own list, so nothing is missed and nothing outside the docs is hit.

  Anchors: every Sphinx section is <section id="x"><h2>..<a class="headerlink"
  href="#x">, so every section gets a real #id. (On the WordPress site only
  6% do.) The id is moved onto the heading so anchors.inline_heading_ids, the
  same code the WordPress path uses, picks it up.

  Dates: the HTML has none. Each page's last commit on `master` in the public
  source repo is used, from one metadata-only clone (no file contents).

  URLs are Sphinx `html` builder files (gpus/gpuoverview.html), so section
  links are page.html#id -- not the WordPress form page/#id.
"""

import json
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from config import get_settings

from . import anchors as anchors_mod
from .http import session
from .sections import split_sections
from .urls import host_of, normalize

logger = logging.getLogger(__name__)

CONTENT_TYPE = "doc"
_VERSIONED = re.compile(r"^/en/[^/]+(?:/(.*))?$")


@dataclass
class DocPage:
    docname: str
    url: str                 # canonical: https://rc-docs.../en/latest/<docname>.html
    title: str = ""
    html: str = ""           # cleaned <article> body
    last_modified: str = ""


@dataclass
class DocsReport:
    base: str = ""
    pages: int = 0
    pages_without_sections: List[str] = field(default_factory=list)
    sections: int = 0
    anchor_kinds: Dict[str, int] = field(default_factory=dict)
    links_to_docs: int = 0
    links_to_rc_website: int = 0
    rc_website_links_to_docs: int = 0
    unresolved_links: List[str] = field(default_factory=list)
    dated_pages: int = 0


def docs_host() -> str:
    return host_of(get_settings().docs_base)


def canonical_doc_url(url: str, known=None) -> Optional[str]:
    """Map any rc-docs page URL (any version, dir or file form, with or without
    a fragment) to the canonical latest-version file URL. None if it is not a
    docs page (downloads, images, the RTD API).

    `/en/latest/software` is ambiguous -- software.html or software/index.html
    -- because URL normalization drops the trailing slash. With `known` (the
    set of real page URLs) the one that exists is chosen."""
    s = get_settings()
    p = urlparse(url)
    if host_of(url) != docs_host():
        return None
    m = _VERSIONED.match(p.path)
    if not m:
        return None
    rest = m.group(1) or ""
    if rest.startswith(("_", "genindex", "search")):
        return None                          # _images/, _downloads/, _static/, generated pages
    if rest == "" or rest.endswith("/"):
        rest += "index.html"
    elif not rest.endswith(".html"):
        if "." in rest.rsplit("/", 1)[-1]:
            return None                      # a file: .pdf, .png, ...
        base = s.docs_base.rstrip("/") + "/"
        as_file, as_dir = base + rest + ".html", base + rest + "/index.html"
        if known is not None and as_file not in known and as_dir in known:
            return as_dir
        return as_file
    return s.docs_base.rstrip("/") + "/" + rest


def discover() -> List[DocPage]:
    """Every page in the latest build, from its search index."""
    s = get_settings()
    r = session().get(s.docs_base.rstrip("/") + "/searchindex.js")
    r.raise_for_status()
    body = r.text
    idx = json.loads(body[body.index("(") + 1: body.rindex(")")])
    pages = [DocPage(d, s.docs_base.rstrip("/") + "/" + d + ".html") for d in idx["docnames"]]
    logger.info("docs: %d pages in %s", len(pages), s.docs_base)
    return pages


def _clean_article(html: str):
    """(title, cleaned article HTML) from a full Sphinx page."""
    soup = BeautifulSoup(html, "lxml")
    art = soup.find("article", attrs={"role": "main"}) or soup.body
    for a in art.select("a.headerlink"):
        a.decompose()                        # the pilcrow would end up in headings and text
    h1 = art.find("h1")
    title = h1.get_text(" ", strip=True) if h1 else ""
    if h1:
        h1.decompose()                       # it is the Page title, not section content
    # Sphinx puts the id on the wrapping <section>; move it to that section's
    # own heading, where the shared anchor code looks for it.
    for sec in art.find_all("section", id=True):
        head = sec.find(["h2", "h3", "h4"], recursive=False)
        if head is not None and not head.get("id"):
            head["id"] = sec["id"]
    return title, str(art)


def _last_modified(pages: List[DocPage]) -> int:
    """Fill last_modified from the source repo's history. Best-effort: on any
    failure pages stay undated, which only weakens recency hints."""
    s = get_settings()
    try:
        with tempfile.TemporaryDirectory(prefix="rc_kg_docs_git_") as tmp:
            subprocess.run(["git", "clone", "-q", "--bare", "--filter=blob:none",
                            "--single-branch", "--branch", s.docs_branch, s.docs_repo, tmp],
                           check=True, capture_output=True, timeout=180)
            files = subprocess.run(["git", "-C", tmp, "ls-tree", "-r", "--name-only", s.docs_branch,
                                    s.docs_source_dir], check=True, capture_output=True,
                                   text=True, timeout=60).stdout.split()
            by_doc = {}
            for f in files:
                stem, _, ext = f[len(s.docs_source_dir) + 1:].rpartition(".")
                if ext in ("md", "rst", "ipynb"):
                    by_doc[stem] = f
            n = 0
            for p in pages:
                f = by_doc.get(p.docname)
                if not f:
                    continue
                out = subprocess.run(["git", "-C", tmp, "log", "-1", "--format=%cI", s.docs_branch,
                                      "--", f], capture_output=True, text=True, timeout=60).stdout.strip()
                if out:
                    p.last_modified = out[:19]
                    n += 1
            return n
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("docs: could not read dates from %s (%s); pages stay undated", s.docs_repo, exc)
        return 0


def section_id(docname: str, order: int) -> str:
    return "%s-%s-%03d" % (CONTENT_TYPE, docname, order)


def collect(pages: List[DocPage], resolve_rc) -> Dict:
    """Fetch, section and link every docs page.

    `resolve_rc(url)` is the WordPress ingest's own redirect-following
    resolver, so a docs link to rc.northeastern.edu lands on the same Page
    node a website link would (or None if it is broken).
    """
    rep = DocsReport(base=get_settings().docs_base, pages=len(pages))
    doc_urls = {p.url for p in pages}
    for p in pages:
        r = session().get(p.url)
        if r.status_code != 200:
            logger.warning("docs: %s returned HTTP %d", p.url, r.status_code)
            continue
        p.title, p.html = _clean_article(r.text)
    rep.dated_pages = _last_modified(pages)

    out = {"pages": [], "sections": [], "next": [], "links_to": [], "external": {}, "links_out": []}
    for p in pages:
        out["pages"].append({
            "url": p.url, "wp_id": None, "content_type": CONTENT_TYPE,
            "title": p.title or p.docname, "published": "", "last_modified": p.last_modified,
            "slug": p.docname, "source": "rtd", "site": docs_host(),
        })
        parsed = split_sections(p.html)
        if not parsed:
            rep.pages_without_sections.append(p.url)
            continue
        ids = anchors_mod.inline_heading_ids(p.html)
        prev = None
        for sec in parsed:
            sid = section_id(p.docname, sec.order)
            hid = ids.get(anchors_mod._key(sec.heading)) if sec.heading_level else None
            if sec.heading_level == 0:
                url, kind = p.url, anchors_mod.ANCHOR_NONE
            elif hid:
                url, kind = "%s#%s" % (p.url, hid), anchors_mod.ANCHOR_ID
            else:                              # not seen on this site; kept for safety
                frag = anchors_mod.text_fragment(sec.heading)
                url, kind = ("%s#:~:text=%s" % (p.url, frag), anchors_mod.ANCHOR_TEXT_FRAGMENT) \
                    if frag else (p.url, anchors_mod.ANCHOR_NONE)
            rep.anchor_kinds[kind] = rep.anchor_kinds.get(kind, 0) + 1
            out["sections"].append({
                "section_id": sid, "page_url": p.url, "heading": sec.heading,
                "heading_level": sec.heading_level, "text": sec.text,
                "url_with_anchor": url, "anchor_kind": kind,
                "content_hash": sec.content_hash, "order": sec.order,
                "char_count": len(sec.text), "retrievable": True,
            })
            rep.sections += 1
            if prev:
                out["next"].append({"from_id": prev, "to_id": sid})
            prev = sid

            for href in sec.hrefs:
                absolute = urljoin(p.url, href)
                if absolute.split("#", 1)[0] == p.url:
                    continue                   # in-page jump
                docs_target = canonical_doc_url(absolute, doc_urls)
                if docs_target:
                    if docs_target in doc_urls:
                        out["links_to"].append({"section_id": sid, "target_url": docs_target,
                                                "href_as_written": href, "via_redirect": False})
                        rep.links_to_docs += 1
                    else:
                        rep.unresolved_links.append(absolute)
                    continue
                norm = normalize(absolute)
                if not norm:
                    continue
                if host_of(norm) == "rc.northeastern.edu":
                    target = resolve_rc(norm)
                    if target:
                        out["links_to"].append({"section_id": sid, "target_url": target,
                                                "href_as_written": href, "via_redirect": target != norm})
                        rep.links_to_rc_website += 1
                    else:
                        rep.unresolved_links.append(norm)
                    continue
                out["external"][norm] = {"url": norm, "domain": host_of(norm)}
                out["links_out"].append({"section_id": sid, "url": norm})
    rep.unresolved_links = sorted(set(rep.unresolved_links))
    out["report"] = rep
    return out
