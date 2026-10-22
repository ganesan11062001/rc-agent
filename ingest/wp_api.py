"""WordPress REST API client.

Pagination follows `X-WP-TotalPages` rather than looping until an empty page:
an empty page is also what a transient 5xx behind a cache looks like, and
stopping early there would silently truncate the corpus.
"""

import html
import re
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional

from config import get_settings

from .http import session

logger = logging.getLogger(__name__)

# Confirmed against /wp-json/wp/v2/types on 2026-10-05. `calendar_event` is its
# own rest_base -- NOT "calendar_events", which is the obvious guess and wrong.
CONTENT_TYPES: Dict[str, str] = {
    "page": "pages",
    "post": "posts",
    "calendar_event": "calendar_event",
}

FIELDS = "id,title,link,content,parent,date,modified,categories,slug,status"


@dataclass
class WPItem:
    wp_id: int
    content_type: str
    title: str
    link: str
    content_html: str
    parent: int
    published: str
    last_modified: str
    categories: List[int] = field(default_factory=list)
    slug: str = ""

    @classmethod
    def from_api(cls, raw: Dict, content_type: str) -> "WPItem":
        return cls(
            wp_id=int(raw["id"]),
            content_type=content_type,
            # Titles arrive HTML-escaped ("AICR: FAQ &#8211; ..."), so decode
            # once here and never again downstream.
            title=html.unescape((raw.get("title") or {}).get("rendered", "")).strip(),
            link=raw.get("link", ""),
            content_html=(raw.get("content") or {}).get("rendered", "") or "",
            parent=int(raw.get("parent") or 0),
            published=raw.get("date") or "",
            last_modified=raw.get("modified") or "",
            categories=list(raw.get("categories") or []),
            slug=raw.get("slug") or "",
        )


def discover_types() -> Dict[str, Dict]:
    """GET /types. Returns the raw map so the report can show it verbatim."""
    s = get_settings()
    data, _ = session().get_json("%s/types" % s.wp_api)
    return data


def fetch_type(content_type: str, modified_after: Optional[str] = None
               ) -> Iterator[WPItem]:
    """Yield every published item of one content type.

    `modified_after` drives the Phase 4 incremental sync; omitted it is a full
    pull.
    """
    s = get_settings()
    endpoint = CONTENT_TYPES[content_type]
    page = 1
    total_pages = 1

    while page <= total_pages:
        params = {"per_page": 100, "page": page, "_fields": FIELDS}
        if modified_after:
            params["modified_after"] = modified_after
        data, headers = session().get_json("%s/%s" % (s.wp_api, endpoint), params)

        if page == 1:
            total_pages = int(headers.get("X-WP-TotalPages") or 1)
            total = headers.get("X-WP-Total") or "?"
            logger.info(
                "%s: %s items across %d page(s)", content_type, total, total_pages
            )

        if not isinstance(data, list):
            raise RuntimeError(
                "%s page %d returned %s, not a list" % (content_type, page, type(data))
            )

        for raw in data:
            yield WPItem.from_api(raw, content_type)
        page += 1


def fetch_categories() -> Dict[int, str]:
    """id -> name for the `category` taxonomy."""
    s = get_settings()
    out: Dict[int, str] = {}
    page, total_pages = 1, 1
    while page <= total_pages:
        data, headers = session().get_json(
            "%s/categories" % s.wp_api,
            {"per_page": 100, "page": page, "_fields": "id,name"},
        )
        if page == 1:
            total_pages = int(headers.get("X-WP-TotalPages") or 1)
        for c in data:
            out[int(c["id"])] = html.unescape(c.get("name", ""))
        page += 1
    return out


def fetch_event_categories() -> Dict[int, str]:
    """The event taxonomy, if the REST API exposes it.

    The sitemap advertises `calendar_event_category`, but a taxonomy can appear
    in a sitemap without being registered `show_in_rest`. A 404 here is an
    expected outcome, not an error.
    """
    s = get_settings()
    try:
        data, _ = session().get_json(
            "%s/calendar_event_category" % s.wp_api,
            {"per_page": 100, "_fields": "id,name"},
        )
    except Exception as exc:                      # noqa: BLE001
        logger.info("event category taxonomy not exposed over REST (%s)", exc)
        return {}
    return {int(c["id"]): html.unescape(c.get("name", "")) for c in data}


_JSON_ALTERNATE = re.compile(
    r"""<link[^>]+rel=["']alternate["'][^>]+type=["']application/json["'][^>]+href=["']([^"']+)["']""",
    re.I,
)


def fetch_from_page(page_url: str, content_type: str) -> Optional[WPItem]:
    """Recover an item the collection endpoint does not list.

    The `calendar_event` collection omits current events, and a `?slug=` query
    for them returns []. But each live page advertises its own record in a
    `<link rel="alternate" type="application/json">`, and fetching that by id
    returns the full published item. Two requests per item, only for the ones
    the sitemap has and the API listing does not.
    """
    r = session().get(page_url + "/")
    if r.status_code != 200:
        logger.warning("sitemap-only %s returned HTTP %d", page_url, r.status_code)
        return None
    m = _JSON_ALTERNATE.search(r.text)
    if not m:
        logger.warning("sitemap-only %s advertises no REST record", page_url)
        return None
    raw, _ = session().get_json(html.unescape(m.group(1)), {"_fields": FIELDS})
    if raw.get("status") != "publish":
        logger.warning("sitemap-only %s is %r, not published", page_url, raw.get("status"))
        return None
    return WPItem.from_api(raw, content_type)
