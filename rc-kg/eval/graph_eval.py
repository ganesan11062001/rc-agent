"""Graph evaluation: are the relations true, and are they useful?

  scripts/with_kg.sh -m eval.graph_eval              integrity, usefulness, topics, topology
  scripts/with_kg.sh -m eval.graph_eval --live       + check links and anchors on the live sites
  scripts/with_kg.sh -m eval.graph_eval --json out.json

1. Integrity -- every relation obeys its invariants (one page per section,
   NEXT is the reading order, LINKS_TO hits real pages, MENTIONS is literally
   in the text, duplicate_of points at a retrievable canonical copy, ...).
   Each check has an expected value; any deviation is a FAIL.
2. Live validity (--live) -- the relations are true *of the sites*: every
   cited section URL lands where it claims (the #id exists on the page, the
   text fragment's heading is on the page), and every LINKS_TO edge's href
   really resolves to its target page. ~1 polite request/s.
3. Usefulness -- do relations put answer-bearing pages in front of the
   model? Page recall@k from the retrieved sections alone, vs. with the pages
   they LINKS_TO (shown to the model as "Related RC pages"), and retrieval
   with the topic signal on vs. off.
4. Topics -- coverage, specificity (IDF), topics too broad to inform, and
   which alias does the matching (a topic whose hits are mostly one generic
   alias is weaker than it looks).
5. Topology -- connected components, pages nothing links to, cross-site
   edges.
"""

import argparse
import json
import logging
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List
from urllib.parse import unquote, urljoin

import yaml
from bs4 import BeautifulSoup

from config import get_settings
from enrich.topics import load_topics
from graph.connection import driver
from retrieval import graphrag

from . import metrics as m

QUESTIONS = Path(__file__).resolve().parent / "questions.yaml"


def _rows(q: str, **params) -> List[Dict]:
    with driver().session(database=get_settings().neo4j_database) as s:
        return [dict(r) for r in s.run(q, **params)]


def _one(q: str, **params):
    rows = _rows(q, **params)
    return list(rows[0].values())[0] if rows else None


# ── 1. integrity ────────────────────────────────────────────────────────────
INTEGRITY = [
    # (name, cypher returning one number, expected)
    ("every section belongs to exactly one page",
     "MATCH (s:Section) OPTIONAL MATCH (p:Page)-[:HAS_SECTION]->(s) WITH s, count(p) AS n "
     "WHERE n <> 1 RETURN count(s)", 0),
    ("section URL is on its own page",
     "MATCH (p:Page)-[:HAS_SECTION]->(s) WHERE NOT s.url_with_anchor STARTS WITH p.url RETURN count(s)", 0),
    ("NEXT links consecutive sections of the same page",
     "MATCH (pa:Page)-[:HAS_SECTION]->(a)-[:NEXT]->(b)<-[:HAS_SECTION]-(pb:Page) "
     "WHERE pa <> pb OR b.order <> a.order + 1 RETURN count(*)", 0),
    ("NEXT count = sections - pages with sections",
     "MATCH (p:Page)-[:HAS_SECTION]->(s) WITH p, count(s) AS c WITH sum(c - 1) AS want "
     "MATCH ()-[n:NEXT]->() WITH want, count(n) AS got RETURN abs(want - got)", 0),
    ("no section has two NEXT",
     "MATCH (a:Section)-[r:NEXT]->() WITH a, count(r) AS n WHERE n > 1 RETURN count(a)", 0),
    ("CHILD_OF has no cycles",
     "MATCH (p:Page)-[:CHILD_OF*1..]->(p) RETURN count(p)", 0),
    ("every LINKS_TO edge records the href it came from",
     "MATCH ()-[r:LINKS_TO]->() WHERE r.href_as_written IS NULL OR r.href_as_written = '' RETURN count(r)", 0),
    ("no ExternalLink is actually an ingested page",
     "MATCH (e:ExternalLink), (p:Page) WHERE e.url = p.url RETURN count(e)", 0),
    ("no orphan ExternalLink",
     "MATCH (e:ExternalLink) WHERE NOT ()-[:LINKS_OUT]->(e) RETURN count(e)", 0),
    # The canonical copy is retrievable unless no copy is (text that appears
    # only on event pages, which are non-retrievable by policy).
    ("duplicate_of points at a canonical copy that is retrievable when any copy is",
     "MATCH (s:Section) WHERE s.duplicate_of IS NOT NULL "
     "OPTIONAL MATCH (c:Section {section_id: s.duplicate_of}) "
     "WITH s, c WHERE c IS NULL OR c.duplicate_of IS NOT NULL OR (NOT c.retrievable AND "
     "EXISTS { MATCH (y:Section {content_hash: c.content_hash}) WHERE y.retrievable }) "
     "RETURN count(s)", 0),
    ("no two retrievable sections share text",
     "MATCH (s:Section {retrievable: true}) WITH s.content_hash AS h, count(*) AS n WHERE n > 1 RETURN count(h)", 0),
    ("no two retrievable sections share a URL",
     "MATCH (s:Section {retrievable: true}) WITH s.url_with_anchor AS u, count(*) AS n WHERE n > 1 RETURN count(u)", 0),
    ("every section has an embedding of the configured size",
     "MATCH (s:Section) WHERE s.embedding IS NULL OR size(s.embedding) <> $dim RETURN count(s)", 0),
    ("every page has a site",
     "MATCH (p:Page) WHERE p.site IS NULL RETURN count(p)", 0),
    ("docs sections link by #id (or are the page overview)",
     "MATCH (p:Page {content_type: 'doc'})-[:HAS_SECTION]->(s) WHERE s.anchor_kind = 'text_fragment' RETURN count(s)", 0),
]


def integrity() -> List[Dict]:
    out = []
    dim = get_settings().embedding_dim
    for name, q, want in INTEGRITY:
        got = _one(q, dim=dim)
        got = 0 if got is None else got
        out.append({"check": name, "expected": want, "got": got, "ok": got == want})
    # MENTIONS must be literally in the text: re-run the matcher and compare.
    topics = {t.name: t for t in load_topics()}
    bad = 0
    rows = _rows("MATCH (s:Section)-[m:MENTIONS]->(t:Topic) RETURN s.heading AS h, s.text AS x, t.name AS t, m.hits AS hits")
    for r in rows:
        if len(topics[r["t"]].pattern.findall("%s\n%s" % (r["h"] or "", r["x"] or ""))) != r["hits"]:
            bad += 1
    out.append({"check": "every MENTIONS edge matches its alias count in the text (%d edges)" % len(rows),
                "expected": 0, "got": bad, "ok": bad == 0})
    return out


# ── 2. live validity ────────────────────────────────────────────────────────
def live_validity(max_pages: int = 0) -> Dict:
    from ingest import rtd
    from ingest.http import session
    from ingest.urls import normalize

    secs = _rows("MATCH (p:Page)-[:HAS_SECTION]->(s:Section {retrievable: true}) "
                 "RETURN p.url AS page, p.site AS site, s.heading AS heading, "
                 "s.url_with_anchor AS url, s.anchor_kind AS kind")
    pages = sorted({r["page"] for r in secs})
    if max_pages:
        pages = pages[:max_pages]
    html, text, status = {}, {}, {}
    for p in pages:
        fetch = p if p.endswith(".html") else p + "/"
        r = session().get(fetch)
        status[p] = r.status_code
        if r.status_code == 200:
            html[p] = r.text
            soup = BeautifulSoup(r.text, "lxml")
            for junk in soup.find_all(["script", "style", "noscript"]):
                junk.decompose()
            text[p] = re.sub(r"\s+", " ", soup.get_text(" ")).casefold()

    anchors = Counter()
    broken: List[str] = []
    for r in secs:
        p = r["page"]
        if p not in status:
            continue
        if status[p] != 200:
            anchors["page_not_200"] += 1; broken.append(r["url"]); continue
        frag = r["url"].split("#", 1)[1] if "#" in r["url"] else ""
        if r["kind"] == "id":
            ok = re.search(r'id=["\']%s["\']' % re.escape(frag), html[p]) is not None
        elif r["kind"] == "text_fragment":
            start = unquote(frag[len(":~:text="):].split(",", 1)[0])
            ok = re.sub(r"\s+", " ", start).casefold() in text[p]
        else:
            ok = True
        anchors["ok" if ok else "missing_on_page"] += 1
        if not ok:
            broken.append(r["url"])

    edges = _rows("MATCH (p:Page)-[:HAS_SECTION]->(:Section)-[l:LINKS_TO]->(t:Page) "
                  "RETURN p.url AS src, t.url AS target, l.href_as_written AS href")
    checked, bad_links, cache = 0, [], {}
    for e in edges:
        if max_pages and e["src"] not in status:
            continue
        absolute = urljoin(e["src"] if e["src"].endswith(".html") else e["src"] + "/", e["href"])
        if absolute not in cache:
            code, final = session().head_resolved(absolute)
            fin = rtd.canonical_doc_url(final) or normalize(final) or final
            cache[absolute] = (code, fin.split("#", 1)[0])
        code, final = cache[absolute]
        checked += 1
        # A website link to /en/explorer-main/... is mapped to the latest page
        # on purpose; it resolves live to the old version, which is not wrong.
        target_equiv = rtd.canonical_doc_url(final) or final
        if code != 200 or target_equiv != e["target"]:
            bad_links.append("%s -> %s (HTTP %s, lands %s)" % (e["src"], e["href"], code, final))
    n_anchor = sum(anchors.values())
    return {
        "pages_checked": len(status),
        "pages_not_200": sorted(p for p, c in status.items() if c != 200),
        "section_links": dict(anchors),
        "section_link_validity": round(anchors["ok"] / n_anchor, 4) if n_anchor else None,
        "broken_section_links": broken[:50],
        "links_to_checked": checked,
        "links_to_validity": round(1 - len(bad_links) / checked, 4) if checked else None,
        "bad_links_to": bad_links[:50],
    }


# ── 3. usefulness ───────────────────────────────────────────────────────────
def usefulness(k: int = 5, sites=None) -> Dict:
    qs = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    if sites:
        qs = [q for q in qs if any(u.split("/")[2] in sites for u in q["expected_urls"])]
    page = lambda u: u.split("#", 1)[0].rstrip("/")
    with_links, without, rescued = [], [], []
    modes = {}
    for mode in ("idf", "off"):
        graphrag._TOPIC_MODE = mode
        rr, hits = [], []
        for q in qs:
            exp = {page(u) for u in q["expected_urls"]}
            res = graphrag.retrieve(q["question"], k=k)
            pages = [page(s.page_url) for s in res.sources]
            v = m.verdicts(pages, exp)
            rr.append(m.reciprocal_rank(v)); hits.append(m.hit_at(v, k))
            if mode == "idf":
                linked = {page(l["url"]) for s in res.sources for l in s.links}
                base = len(exp & set(pages)) / len(exp)
                plus = len(exp & (set(pages) | linked)) / len(exp)
                without.append(base); with_links.append(plus)
                if not (exp & set(pages)) and (exp & linked):
                    rescued.append(q["question"])
        modes[mode] = {"mrr": m.bootstrap_ci(rr), "hit@%d" % k: m.bootstrap_ci(hits)}
    graphrag._TOPIC_MODE = "idf"
    fmt = lambda t: {"mean": round(t[0], 3), "ci95": [round(t[1], 3), round(t[2], 3)]}
    return {
        "page_recall@%d_sources_only" % k: fmt(m.bootstrap_ci(without)),
        "page_recall@%d_with_linked_pages" % k: fmt(m.bootstrap_ci(with_links)),
        "questions_answered_only_via_a_link": rescued,
        "topic_signal_on_idf": {n: fmt(v) for n, v in modes["idf"].items()},
        "topic_signal_off": {n: fmt(v) for n, v in modes["off"].items()},
    }


# ── 4. topics ───────────────────────────────────────────────────────────────
def topics_quality() -> Dict:
    n = _one("MATCH (s:Section {retrievable: true}) RETURN count(s)")
    per = _rows("MATCH (t:Topic) OPTIONAL MATCH (s:Section {retrievable: true})-[:MENTIONS]->(t) "
                "RETURN t.name AS topic, count(s) AS df ORDER BY df DESC")
    covered = _one("MATCH (s:Section {retrievable: true}) WHERE (s)-[:MENTIONS]->() RETURN count(s)")
    texts = _rows("MATCH (s:Section {retrievable: true}) RETURN s.heading + '\n' + s.text AS x")
    alias_hits: Dict[str, Counter] = defaultdict(Counter)
    for t in load_topics():
        for a in t.aliases:
            pat = re.compile(r"(?<![\w/.-])%s(?![\w-])" % re.escape(a), re.I)
            alias_hits[t.name][a] = sum(len(pat.findall(r["x"] or "")) for r in texts)
    table = []
    for r in per:
        idf = math.log(n / r["df"]) / math.log(n) if r["df"] else 0.0
        hits = alias_hits[r["topic"]]
        total = sum(hits.values()) or 1
        top_alias, top_n = (hits.most_common(1)[0] if hits else ("", 0))
        table.append({"topic": r["topic"], "sections": r["df"], "share": round(r["df"] / n, 3),
                      "idf": round(idf, 3), "top_alias": top_alias,
                      "top_alias_share_of_hits": round(top_n / total, 2)})
    return {
        "retrievable_sections": n,
        "covered_by_a_topic": round(covered / n, 3),
        "too_broad (in >25% of sections)": [t["topic"] for t in table if t["share"] > 0.25],
        "never_matched": [t["topic"] for t in table if t["sections"] == 0],
        # A topic matched almost entirely by one alias is only as specific as
        # that alias -- unless the alias is the topic's own name (MATLAB).
        "dominated_by_one_alias (>80% of hits)": [
            "%s ('%s')" % (t["topic"], t["top_alias"]) for t in table
            if t["top_alias_share_of_hits"] > 0.8 and t["sections"] > 10
            and t["top_alias"].casefold() != t["topic"].casefold()],
        "table": table,
    }


# ── 5. topology ─────────────────────────────────────────────────────────────
def topology() -> Dict:
    pages = [r["u"] for r in _rows("MATCH (p:Page) RETURN p.url AS u")]
    edges = _rows("MATCH (a:Page)-[:HAS_SECTION]->(:Section)-[:LINKS_TO]->(b:Page) WHERE a <> b "
                  "RETURN DISTINCT a.url AS a, b.url AS b, a.site AS sa, b.site AS sb "
                  "UNION MATCH (a:Page)-[:CHILD_OF]->(b:Page) RETURN a.url AS a, b.url AS b, a.site AS sa, b.site AS sb")
    parent = {p: p for p in pages}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for e in edges:
        parent[find(e["a"])] = find(e["b"])
    comps = Counter(find(p) for p in pages)
    inbound = {e["b"] for e in edges}
    retrievable_pages = {r["u"] for r in _rows(
        "MATCH (p:Page)-[:HAS_SECTION]->(:Section {retrievable: true}) RETURN DISTINCT p.url AS u")}
    no_inbound = sorted(p for p in retrievable_pages if p not in inbound)
    cross = Counter("%s -> %s" % (e["sa"], e["sb"]) for e in edges if e["sa"] != e["sb"])
    return {
        "pages": len(pages), "page_edges": len(edges),
        "components": len(comps), "largest_component": max(comps.values()) if comps else 0,
        "singleton_pages": sum(1 for c in comps.values() if c == 1),
        "retrievable_pages_nothing_links_to": len(no_inbound),
        "examples_nothing_links_to": no_inbound[:8],
        "cross_site_page_edges": dict(cross),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="check links and anchors on the live sites")
    ap.add_argument("--max-pages", type=int, default=0, help="--live: only the first N pages")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--sites", default="", help="usefulness: only questions answerable from these hosts")
    ap.add_argument("--json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("neo4j").setLevel(logging.ERROR)

    rep = {"integrity": integrity()}
    print("1. INTEGRITY")
    for c in rep["integrity"]:
        print("   %-4s %-70s got %s" % ("ok" if c["ok"] else "FAIL", c["check"], c["got"]))
    if args.live:
        rep["live"] = live_validity(args.max_pages)
        L = rep["live"]
        print("2. LIVE VALIDITY  (%d pages)" % L["pages_checked"])
        print("   section links land correctly: %s  %s" % (L["section_link_validity"], L["section_links"]))
        for b in L["broken_section_links"][:10]:
            print("     broken: %s" % b)
        print("   LINKS_TO edges resolve to their target: %s of %d" % (L["links_to_validity"], L["links_to_checked"]))
        for b in L["bad_links_to"][:10]:
            print("     bad: %s" % b)
        if L["pages_not_200"]:
            print("   pages not 200: %s" % L["pages_not_200"])
    rep["usefulness"] = usefulness(args.k, [x for x in args.sites.split(",") if x] or None)
    U = rep["usefulness"]
    print("3. USEFULNESS")
    for key in ("page_recall@%d_sources_only" % args.k, "page_recall@%d_with_linked_pages" % args.k):
        print("   %-38s %.3f %s" % (key, U[key]["mean"], U[key]["ci95"]))
    print("   answered only via a linked page: %s" % (U["questions_answered_only_via_a_link"] or "none"))
    for mode in ("topic_signal_on_idf", "topic_signal_off"):
        print("   %-20s %s" % (mode, {n: "%.3f %s" % (v["mean"], v["ci95"]) for n, v in U[mode].items()}))
    rep["topics"] = topics_quality()
    T = rep["topics"]
    print("4. TOPICS  coverage %.1f%% of %d retrievable sections" % (100 * T["covered_by_a_topic"], T["retrievable_sections"]))
    for key in ("too_broad (in >25% of sections)", "never_matched", "dominated_by_one_alias (>80% of hits)"):
        print("   %-40s %s" % (key, T[key] or "none"))
    rep["topology"] = topology()
    print("5. TOPOLOGY  %s" % {k2: v for k2, v in rep["topology"].items() if k2 != "examples_nothing_links_to"})
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=2, default=str), encoding="utf-8")
    return 1 if not all(c["ok"] for c in rep["integrity"]) else 0


if __name__ == "__main__":
    sys.exit(main())
