"""Phase 1 orchestrator: WP API (+ rc-docs, ingest/rtd.py) -> sections -> anchors -> links -> Neo4j.

Deliberately contains no LLM call and no embedding call. Phase 1 has to be
runnable, repeatable and cheap, so that the parts most likely to be wrong --
sectioning, anchors, link resolution -- can be inspected before any money or
GPU time is spent on them.

Run:  python -m ingest.run --report data/phase1_report.json
"""

import argparse
import json
import logging
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from config import get_settings
from graph import schema, writer

from . import anchors as anchors_mod
from . import rtd
from . import sitemap as sitemap_mod
from . import wp_api
from .http import session
from .sections import split_sections
from .urls import classify, is_wp_api_url, normalize

logger = logging.getLogger("ingest")


@dataclass
class Report:
    """The Phase 1 ingest report, written as JSON by ingest(): per-type counts,
    anchor and link statistics, sitemap cross-check, warnings, graph census."""

    started_at: str = ""
    finished_at: str = ""
    types_endpoint: Dict[str, str] = field(default_factory=dict)
    items_per_type: Dict[str, int] = field(default_factory=dict)
    sections_per_type: Dict[str, int] = field(default_factory=dict)
    section_size: Dict[str, int] = field(default_factory=dict)
    anchor_kinds: Dict[str, int] = field(default_factory=dict)
    pages_without_any_anchor: List[str] = field(default_factory=list)
    sitemap_vs_api: Dict[str, Dict[str, List[str]]] = field(default_factory=dict)
    recovered_from_sitemap: List[str] = field(default_factory=list)
    docs: Dict = field(default_factory=dict)
    duplicate_sections: Dict[str, int] = field(default_factory=dict)
    not_retrievable: Dict = field(default_factory=dict)
    internal_links: Dict[str, int] = field(default_factory=dict)
    redirected_links: Dict[str, str] = field(default_factory=dict)
    broken_internal_links: List[str] = field(default_factory=list)
    external_domains: Dict[str, int] = field(default_factory=dict)
    graph_counts: Dict[str, int] = field(default_factory=dict)
    http_requests: int = 0
    warnings: List[str] = field(default_factory=list)


def section_id(content_type: str, wp_id: int, order: int) -> str:
    """Stable Section id for the `order`-th section of a WordPress item."""
    return "%s-%d-%03d" % (content_type, wp_id, order)



# Docs first: when the same text is on both sites, the docs page is the
# maintained technical reference.
_TYPE_RANK = {rtd.CONTENT_TYPE: 0, "page": 1, "post": 2, "calendar_event": 3}


def _mark_duplicates(rows: List[Dict], ctype: Dict[str, str]) -> Dict[str, int]:
    """Keep one retrievable copy of each repeated section.

    Footers ("How Can Research Computing Support You?" on 26 pages) and staff
    bios repeated on every event page would otherwise fill the top-k with the
    same text under different URLs. All copies stay in the graph, so links and
    NEXT chains are intact; only the canonical one is retrievable, and the rest
    point at it via `duplicate_of`. Canonical = a retrievable copy on a docs
    page, then a `page`, then a post or event, then the shallowest URL.
    `ctype` maps page URL -> content type.
    """
    groups: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        r["duplicate_of"] = None
        groups[r["content_hash"]].append(r)

    hidden = 0
    repeated = 0
    for copies in groups.values():
        if len(copies) < 2:
            continue
        repeated += 1
        copies.sort(key=lambda r: (
            not r["retrievable"],
            _TYPE_RANK.get(ctype[r["page_url"]], 9),
            r["page_url"].count("/"),
            r["page_url"],
            r["order"],
        ))
        canonical = copies[0]["section_id"]
        for r in copies[1:]:
            r["duplicate_of"] = canonical
            if r["retrievable"]:
                hidden += 1
            r["retrievable"] = False
    return {"repeated_texts": repeated, "copies_hidden_from_retrieval": hidden}


# Who people are and the AVP's letters: never the answer to a support ticket,
# but retrieved by any ticket that names a person or carries a signature. On
# 19 real tickets (2026-10) "RC Home > Alex Morgan, PhD" and the a researcher
# spotlight were in the top 6 for an access request, a node outage and a GPU
# fault -- a.morgan is on every ticket's CC list, and "Pat" signed one.
NOT_SUPPORT_CATEGORIES = {"Researcher Spotlight", "From the AVP"}
PEOPLE_PAGE_PATHS = ("/research-computing-team", "/from-the-avp")
_TITLES = re.compile(r",?\s*(?:Ph\.?\s?D|M\.?D|M\.?S)\.?$", re.I)
_PERSON = re.compile(r"[A-Z][\w'.-]*(?: \(?[A-Z][\w'.-]*\)?){1,3}")
_NOT_NAME_WORDS = {"research", "computing", "how", "overview", "support", "student", "students",
                   "team", "alumni", "assistants", "office"}


def _person(heading: str) -> Optional[str]:
    """A heading that is a person's name ("Sam Rivera, PhD"), normalised."""
    h = _TITLES.sub("", heading.strip())
    if not _PERSON.fullmatch(h) or {w.strip("()").lower() for w in h.split()} & _NOT_NAME_WORDS:
        return None
    return h.lower()


def _mark_not_support(rows: List[Dict], page_categories: Dict[str, Set[str]]) -> Dict[str, int]:
    """Hide people and news-from-leadership content from retrieval: whole
    pages in NOT_SUPPORT_CATEGORIES or at PEOPLE_PAGE_PATHS, and any section
    elsewhere headed with the name of someone those people pages introduce
    (a bio repeated on the home page or a tips page). Like duplicates, the
    sections stay in the graph; only `retrievable` changes."""
    people_pages = {r["page_url"] for r in rows
                    if r["page_url"].rstrip("/").endswith(PEOPLE_PAGE_PATHS)}
    names = {n for r in rows if r["page_url"] in people_pages for n in [_person(r["heading"])] if n}
    by_page = by_name = 0
    for r in rows:
        if not r["retrievable"]:
            continue
        if r["page_url"] in people_pages or page_categories.get(r["page_url"], set()) & NOT_SUPPORT_CATEGORIES:
            r["retrievable"] = False
            by_page += 1
        elif _person(r["heading"]) in names:
            r["retrievable"] = False
            by_name += 1
    return {"people_or_leadership_pages": by_page, "bios_elsewhere": by_name}


def feed_lead(text: str, post_titles: List[str]) -> Optional[str]:
    """If this text is a feed -- bullets that begin with the titles of posts
    already in the graph -- return only what comes before the first of them.

    The Status Updates page lists every status post in one 11,682-character
    section. As a whole it matched any ticket about storage, maintenance or
    access (6 of 19 real tickets), and only its first 2,500 characters ever
    reached the model. Each update is its own post, with its own date; the
    page's own words are the lead ("The Explorer Cluster is fully operational").
    """
    titles = sorted({t.strip() for t in post_titles if len(t.strip()) >= 12}, key=len, reverse=True)
    lines = text.split("\n")
    hits = [i for i, line in enumerate(lines)
            if line.startswith("- ") and any(line[2:].startswith(t) for t in titles)]
    if len(hits) < 3:
        return None
    return "\n".join(lines[:hits[0]]).strip()


def _resolve_internal(
    href_norm: str, known: Set[str], cache: Dict[str, Tuple[int, str]]
) -> Tuple[Optional[str], bool, Optional[int]]:
    """Map a normalized internal href onto a known Page url.

    Returns (page_url or None, came_via_redirect, http_status or None).
    A third of this site's internal hrefs are 301s, so without this step
    ~30 of 90 LINKS_TO targets would be dropped as 'broken'.
    """
    if href_norm in known:
        return href_norm, False, 200
    if not get_settings().resolve_redirects:
        return None, False, None

    if href_norm not in cache:
        cache[href_norm] = session().head_resolved(href_norm)
    status, final = cache[href_norm]
    final_norm = normalize(final) or final
    if final_norm in known:
        return final_norm, True, status
    return None, False, status


def ingest(clear: bool = False, report_path: Optional[str] = None) -> Report:
    """Run Phase 1 end to end: fetch the website (WP API, cross-checked
    against the sitemap) and, if enabled, the docs site; split sections,
    resolve anchors and links, and write it all to Neo4j.

    Makes network requests and writes to Neo4j; `clear` wipes the graph's
    data first. Writes the report to `report_path` if given, and returns it.
    """
    s = get_settings()
    rep = Report(started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    logger.info("applying schema")
    schema.apply()
    if clear:
        schema.drop_all_data()
        schema.apply()

    # --- step 1: confirm the REST endpoint names --------------------------
    types = wp_api.discover_types()
    for slug, meta in types.items():
        if slug in wp_api.CONTENT_TYPES:
            rep.types_endpoint[slug] = meta.get("rest_base", "")
    for slug, expected in wp_api.CONTENT_TYPES.items():
        actual = rep.types_endpoint.get(slug)
        if actual != expected:
            msg = "rest_base for %r is %r, code expects %r" % (slug, actual, expected)
            logger.error(msg)
            rep.warnings.append(msg)

    # --- step 2/3: fetch items and taxonomies ----------------------------
    items: List[wp_api.WPItem] = []
    for ctype in wp_api.CONTENT_TYPES:
        got = list(wp_api.fetch_type(ctype))
        rep.items_per_type[ctype] = len(got)
        items.extend(got)

    categories = wp_api.fetch_categories()
    event_categories = wp_api.fetch_event_categories()
    all_categories = {**categories, **event_categories}

    by_url = {normalize(i.link) or i.link: i for i in items}
    by_wp = {(i.content_type, i.wp_id): i for i in items}
    known_urls: Set[str] = set(by_url)

    # --- step 5: cross-check against the sitemap -------------------------
    logger.info("fetching sitemap for cross-check")
    sm = sitemap_mod.fetch_sitemap()
    api_urls: Dict[str, Set[str]] = defaultdict(set)
    for url, item in by_url.items():
        api_urls[item.content_type].add(url)
    rep.sitemap_vs_api = sitemap_mod.compare(dict(api_urls), sm)

    # The cross-check is only useful if it changes what gets loaded: fetch
    # what the sitemap has and the API listing does not.
    recovered: Set[str] = set()
    for ctype, diff in rep.sitemap_vs_api.items():
        if ctype not in wp_api.CONTENT_TYPES:
            continue
        for url in diff["only_in_sitemap"]:
            it = wp_api.fetch_from_page(url, ctype)
            if not it:
                rep.warnings.append("sitemap-only %s could not be recovered" % url)
                continue
            key = normalize(it.link) or it.link
            if key in by_url:
                continue
            items.append(it)
            by_url[key] = it
            by_wp[(it.content_type, it.wp_id)] = it
            known_urls.add(key)
            recovered.add(key)
    rep.recovered_from_sitemap = sorted(recovered)
    if recovered:
        logger.info("recovered %d sitemap-only item(s) by id", len(recovered))

    # --- the docs site: list its pages now, so website links to it can
    # become LINKS_TO edges instead of ExternalLinks -----------------------
    doc_pages: List[rtd.DocPage] = rtd.discover() if s.docs_enabled else []
    doc_urls: Set[str] = {p.url for p in doc_pages}
    website_to_docs = 0

    # --- write Page nodes ------------------------------------------------
    page_rows = [
        {
            "url": url,
            "wp_id": it.wp_id,
            "content_type": it.content_type,
            "title": it.title,
            "published": it.published,
            "last_modified": it.last_modified,
            "slug": it.slug,
            "source": "sitemap" if url in recovered else "wp_api",
            "site": "rc.northeastern.edu",
        }
        for url, it in by_url.items()
    ]
    writer.upsert_pages(page_rows)

    if all_categories:
        writer.upsert_categories(
            [
                {
                    "wp_id": cid,
                    "name": name,
                    "taxonomy": "calendar_event_category" if cid in event_categories
                    else "category",
                }
                for cid, name in all_categories.items()
            ]
        )
        writer.link_in_category(
            [
                {"page_url": url, "category_id": cid}
                for url, it in by_url.items()
                for cid in it.categories
                if cid in all_categories
            ]
        )

    # CHILD_OF from the WP parent field.
    child_rows = []
    for url, it in by_url.items():
        if not it.parent:
            continue
        parent = by_wp.get((it.content_type, it.parent))
        if parent:
            child_rows.append(
                {"child_url": url, "parent_url": normalize(parent.link) or parent.link}
            )
        else:
            rep.warnings.append(
                "%s has parent %d which is not in the published set" % (url, it.parent)
            )
    writer.link_child_of(child_rows)

    # --- steps 6/7/8: sections, anchors, links ---------------------------
    section_rows: List[Dict] = []
    next_rows: List[Dict] = []
    links_to_rows: List[Dict] = []
    external_rows: Dict[str, Dict] = {}
    link_out_rows: List[Dict] = []
    sizes: List[int] = []
    anchor_counter: Counter = Counter()
    ext_domains: Counter = Counter()
    internal_counter = Counter()
    redirect_cache: Dict[str, Tuple[int, str]] = {}
    broken: Set[str] = set()
    per_type_sections: Counter = Counter()

    post_titles = [it.title for it in by_url.values() if it.content_type == "post"]
    feeds_trimmed: Dict[str, int] = {}
    for url, it in by_url.items():
        parsed = split_sections(it.content_html)
        if not parsed:
            rep.warnings.append("%s produced no sections" % url)
            continue
        for p in parsed:
            lead = feed_lead(p.text, post_titles) if it.content_type == "page" else None
            if lead:
                feeds_trimmed["%s > %s" % (url, p.heading)] = len(p.text) - len(lead)
                p.text = lead

        inline_ids = anchors_mod.inline_heading_ids(it.content_html)
        # One live fetch per page, only if some heading still needs an id.
        needs_live = any(
            p.heading_level > 0
            and anchors_mod._key(p.heading) not in inline_ids
            for p in parsed
        )
        live_ids = anchors_mod.fetch_live_heading_ids(url + "/") if needs_live else {}

        page_anchor_kinds = []
        seen_headings: Set[str] = set()
        retrievable_page = not (
            it.content_type == "calendar_event"
            and not s.include_events_in_retrieval
        )

        prev_sid = None
        for p in parsed:
            sid = section_id(it.content_type, it.wp_id, p.order)
            anc = anchors_mod.build_anchor(
                url,
                p.heading,
                p.heading_level,
                live_ids,
                inline_ids.get(anchors_mod._key(p.heading)),
            )
            key = anchors_mod._key(p.heading)
            if anc.kind == anchors_mod.ANCHOR_TEXT_FRAGMENT and key in seen_headings:
                anc = anchors_mod.Anchor(anchors_mod.disambiguate(anc.url_with_anchor, p.text), anc.kind)
            seen_headings.add(key)
            anchor_counter[anc.kind] += 1
            page_anchor_kinds.append(anc.kind)
            sizes.append(len(p.text))
            per_type_sections[it.content_type] += 1

            section_rows.append(
                {
                    "section_id": sid,
                    "page_url": url,
                    "heading": p.heading,
                    "heading_level": p.heading_level,
                    "text": p.text,
                    "url_with_anchor": anc.url_with_anchor,
                    "anchor_kind": anc.kind,
                    "content_hash": p.content_hash,
                    "order": p.order,
                    "char_count": len(p.text),
                    "retrievable": retrievable_page,
                }
            )
            if prev_sid:
                next_rows.append({"from_id": prev_sid, "to_id": sid})
            prev_sid = sid

            # step 8: body links
            for href in p.hrefs:
                norm = normalize(href)
                if not norm or is_wp_api_url(norm):
                    continue
                kind, host = classify(norm)
                if kind == "internal":
                    internal_counter[norm] += 1
                    target, via_redirect, status = _resolve_internal(
                        norm, known_urls, redirect_cache
                    )
                    if target:
                        links_to_rows.append(
                            {
                                "section_id": sid,
                                "target_url": target,
                                "href_as_written": href,
                                "via_redirect": via_redirect,
                            }
                        )
                    else:
                        broken.add(norm)
                else:
                    docs_target = rtd.canonical_doc_url(norm, doc_urls) if doc_urls else None
                    if docs_target in doc_urls:
                        # Any version (latest, explorer-main) maps to the
                        # ingested latest page.
                        links_to_rows.append({"section_id": sid, "target_url": docs_target,
                                              "href_as_written": href, "via_redirect": False})
                        website_to_docs += 1
                        continue
                    ext_domains[host] += 1
                    external_rows[norm] = {"url": norm, "domain": host}
                    link_out_rows.append({"section_id": sid, "url": norm})

        if page_anchor_kinds and anchors_mod.ANCHOR_ID not in page_anchor_kinds:
            rep.pages_without_any_anchor.append(url)

    ctype = {url: it.content_type for url, it in by_url.items()}
    if doc_pages:
        logger.info("fetching %d docs pages", len(doc_pages))
        docs = rtd.collect(
            doc_pages,
            resolve_rc=lambda u: _resolve_internal(u, known_urls, redirect_cache)[0],
        )
        writer.upsert_pages(docs["pages"])
        section_rows += docs["sections"]
        next_rows += docs["next"]
        links_to_rows += docs["links_to"]
        external_rows.update(docs["external"])
        link_out_rows += docs["links_out"]
        ctype.update({p["url"]: rtd.CONTENT_TYPE for p in docs["pages"]})
        docs["report"].rc_website_links_to_docs = website_to_docs
        rep.docs = docs["report"].__dict__
        for url in docs["report"].pages_without_sections:
            rep.warnings.append("%s produced no sections" % url)

    page_categories = {url: {all_categories[c] for c in it.categories if c in all_categories}
                       for url, it in by_url.items()}
    rep.not_retrievable = dict(_mark_not_support(section_rows, page_categories),
                               feed_chars_removed=feeds_trimmed)
    rep.duplicate_sections = _mark_duplicates(section_rows, ctype)
    writer.upsert_sections(section_rows)
    writer.link_next(next_rows)
    writer.link_to_pages(links_to_rows)
    writer.upsert_external(list(external_rows.values()))
    writer.link_out(link_out_rows)

    # --- step 10: assemble the report ------------------------------------
    rep.sections_per_type = dict(per_type_sections)
    sizes.sort()
    if sizes:
        rep.section_size = {
            "count": len(sizes),
            "min": sizes[0],
            "p50": sizes[len(sizes) // 2],
            "p90": sizes[int(len(sizes) * 0.9)],
            "max": sizes[-1],
            "under_200_chars": sum(1 for x in sizes if x < 200),
            "over_4000_chars": sum(1 for x in sizes if x > 4000),
        }
    rep.anchor_kinds = dict(anchor_counter)
    rep.internal_links = {
        "href_occurrences": sum(internal_counter.values()),
        "distinct_targets": len(internal_counter),
        "resolved_edges": len(links_to_rows),
        "via_redirect": sum(1 for r in links_to_rows if r["via_redirect"]),
    }
    rep.redirected_links = {
        src: (normalize(final) or final)
        for src, (status, final) in redirect_cache.items()
        if (normalize(final) or final) != src and status == 200
    }
    rep.broken_internal_links = sorted(broken)
    rep.external_domains = dict(ext_domains.most_common())
    rep.graph_counts = writer.counts()
    rep.http_requests = session().request_count
    rep.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(
            json.dumps(rep.__dict__, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        logger.info("report written to %s", report_path)
    return rep


def main() -> int:
    """CLI entry point: parse flags and run ingest()."""
    ap = argparse.ArgumentParser(description="Phase 1 ingest (no LLM, no embeddings)")
    ap.add_argument("--clear", action="store_true", help="wipe the graph first")
    ap.add_argument("--report", default="data/phase1_report.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # The driver logs one multi-line INFO per "IF NOT EXISTS index already
    # exists" notification, which buries the actual progress lines. They carry
    # no information we act on; real failures still raise.
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    ingest(clear=args.clear, report_path=args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
