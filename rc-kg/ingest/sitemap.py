"""Read the WordPress sitemap index, for cross-checking the API.

This is not redundant with the API. Measured on 2026-10-05 the two disagree
completely about events: the API serves 15 events dated Dec 2025 - Jun 2026,
the sitemap lists 15 *different* Fall 2026 events, and asking the API for a
sitemap-only slug returns []. Without this cross-check the ingest would load
only stale events and look successful.
"""

import logging
import re
from dataclasses import dataclass
from typing import Dict, List, Set

from config import get_settings

from .http import session
from .urls import normalize

logger = logging.getLogger(__name__)

# Taxonomy and pattern sitemaps are not content and must not be treated as
# missing pages in the report.
_SKIP_SITEMAPS = ("taxonomies", "coblocks")


@dataclass
class SitemapEntry:
    url: str
    lastmod: str
    sitemap: str          # which child sitemap it came from


def _child_sitemaps(index_xml: str) -> List[str]:
    return re.findall(r"<loc>\s*([^<]+?)\s*</loc>", index_xml)


def _entries(xml: str, source: str) -> List[SitemapEntry]:
    out: List[SitemapEntry] = []
    for block in re.findall(r"(?is)<url>(.*?)</url>", xml):
        loc = re.search(r"<loc>\s*([^<]+?)\s*</loc>", block)
        mod = re.search(r"<lastmod>\s*([^<]+?)\s*</lastmod>", block)
        if not loc:
            continue
        norm = normalize(loc.group(1))
        if norm:
            out.append(SitemapEntry(norm, mod.group(1) if mod else "", source))
    return out


def fetch_sitemap() -> Dict[str, List[SitemapEntry]]:
    """Return {content_type_guess: [entries]} for the content sitemaps only."""
    s = get_settings()
    resp = session().get(s.sitemap_index)
    resp.raise_for_status()

    grouped: Dict[str, List[SitemapEntry]] = {}
    for child in _child_sitemaps(resp.text):
        name = child.rsplit("/", 1)[-1]
        if any(skip in name for skip in _SKIP_SITEMAPS):
            logger.debug("skipping non-content sitemap %s", name)
            continue
        # wp-sitemap-posts-<type>-1.xml
        m = re.match(r"wp-sitemap-posts-(.+?)-\d+\.xml", name)
        ctype = m.group(1) if m else name
        r = session().get(child)
        if r.status_code != 200:
            logger.warning("sitemap %s returned HTTP %d", child, r.status_code)
            continue
        grouped.setdefault(ctype, []).extend(_entries(r.text, name))
    return grouped


def compare(api_urls: Dict[str, Set[str]],
            sitemap: Dict[str, List[SitemapEntry]]) -> Dict[str, Dict[str, List[str]]]:
    """Per content type, what each source has that the other does not."""
    report: Dict[str, Dict[str, List[str]]] = {}
    for ctype in sorted(set(api_urls) | set(sitemap)):
        api = api_urls.get(ctype, set())
        sm = {e.url for e in sitemap.get(ctype, [])}
        report[ctype] = {
            "only_in_api": sorted(api - sm),
            "only_in_sitemap": sorted(sm - api),
            "in_both": sorted(api & sm),
        }
    return report
