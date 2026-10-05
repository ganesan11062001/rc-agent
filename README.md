# RC-KG — grounded answers with exact links, from the RC website

Ask a question in plain English; get an answer built **only** from
rc.northeastern.edu content, plus the specific page sections it came from.

Built for the Northeastern Research Computing team, who need to paste a link to
the page that actually supports the answer they give a researcher.

> **Status:** Phase 1 (ingestion) complete and verified against the live site.
> Phases 2–5 (topics, embeddings, hybrid retrieval, the API, evaluation) are
> not built yet. See [Phase status](#phase-status).

---

## The two rules the design exists to enforce

1. **Nothing is stored without a URL.** Enforced in `graph/writer.py`, which
   refuses a `Page` row with no `url` and a `Section` row with no
   `url_with_anchor`.
2. **The model never writes a URL.** When Phase 3 lands, every link in an
   answer is checked against the set of sections that were actually retrieved,
   and anything else is stripped. An LLM that invents a plausible-looking RC
   URL is worse than one that says "I don't know".

---

## Setup

### Neo4j

**On a laptop, or anywhere with a container runtime:**

```bash
NEO4J_PASSWORD=pick-something docker compose up -d
```

**On Explorer** — the compute nodes have no docker, podman, apptainer or
singularity, so Neo4j runs from a tarball under your own account:

```bash
./scripts/install_neo4j.sh      # ~150 MB into vendor/, once
./scripts/start_neo4j.sh        # picks free ports, writes them into .env
./scripts/start_neo4j.sh --status
./scripts/stop_neo4j.sh
```

Both give you the same Neo4j 5.26 Community, whose built-in vector and
fulltext indexes are all this project needs — no plugins.

Two things worth knowing about the cluster path:

- **The store lives on `/tmp`**, which is node-local XFS. Neo4j does not
  support running its store on NFS, and both `/projects` and `/scratch` are
  NFS. The cost is that the database does not survive the node — which is fine,
  because the whole graph rebuilds from the WordPress API in about 90 seconds.
  `scripts/dump_neo4j.sh` keeps a copy on `/projects` if you want one.
- **It binds `127.0.0.1` only.** To use the browser UI, forward the port:
  `ssh -L <http_port>:127.0.0.1:<http_port> <node>`.

### Python

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # start_neo4j.sh fills in the NEO4J_* lines
```

Embedding and LLM providers are environment variables
(`EMBEDDING_PROVIDER`, `LLM_PROVIDER`). Nothing in the code names a vendor.

---

## Running each phase

```bash
# Phase 1 — ingest. No LLM, no embeddings, ~90 polite requests.
.venv/bin/python -m ingest.run --clear --report data/phase1_report.json

.venv/bin/python -m pytest tests/ -q
```

Phases 2–5 are not implemented yet.

---

## What Phase 1 produced

104 items → 360 sections, in 93 seconds and 90 HTTP requests.

| content type | items | sections |
|---|---|---|
| page | 34 | 219 |
| post | 55 | 112 |
| calendar_event | 15 | 29 |

Graph: `Page=104  Section=360  Category=13  ExternalLink=102`,
`HAS_SECTION=360  NEXT=257  LINKS_TO=247  LINKS_OUT=266  IN_CATEGORY=77  CHILD_OF=14`.

---

## Four things the site does that the code has to work around

**1. Heading anchors barely exist.** Only 21 of 360 sections (6%) get a real
`#id`. The plan assumed the live theme adds ids at render time — it does not; a
live fetch of `/research-computing-policies/` finds zero. So 66% of sections
are linked with a [Scroll-to-Text
Fragment](https://developer.mozilla.org/en-US/docs/Web/URI/Fragment/Text_fragments)
(`#:~:text=…`), which works in Chrome, Edge and Safari 16.1+ **but not
Firefox**, where it silently lands at the top of the page. The remaining 28%
are `Overview` sections, which are the top of the page anyway.

`Section.anchor_kind` records which of the three a link is, so retrieval can
prefer id-anchored sections and the report can tell you how much of the corpus
is only reachable by text fragment. **This is the open design question** — the
real fix is to have the RC web team enable heading anchors in the theme.

**2. The REST API cannot see the current events.** The API and the sitemap have
*zero overlap* on `calendar_event`: the API serves 15 events dated Dec 2025 –
Jun 2026, the sitemap lists 10 different Fall 2026 ones, and asking the API for
a sitemap-only slug returns `[]`. Ingesting from the API alone silently loads
only stale events. This is why the sitemap cross-check is not optional.

**3. A third of internal links are redirects.** 299 internal hrefs point at 80
distinct targets; 123 of the 247 `LINKS_TO` edges only resolve after following
a 301 (`/support/gettinghelp` → `/getting-help` appears 63 times). With
`RESOLVE_REDIRECTS=false` those edges vanish.

**4. The most-linked domain is out of scope.**
`rc-docs.northeastern.edu` gets 80 links — more than double the next domain —
and the scope rule records it as an `ExternalLink` without following it. Worth
revisiting: it is where most technical answers live.

---

## Graph schema

```
(:Page {wp_id, content_type, title, url, published, last_modified, slug})
(:Section {section_id, heading, heading_level, text, url_with_anchor,
           anchor_kind, content_hash, order, char_count, retrievable, embedding})
(:Topic {name, aliases})        (:Category {wp_id, name, taxonomy})
(:ExternalLink {url, domain})

(:Page)-[:HAS_SECTION]->(:Section)     (:Section)-[:NEXT]->(:Section)
(:Page)-[:CHILD_OF]->(:Page)           (:Section)-[:LINKS_TO]->(:Page)
(:Page)-[:IN_CATEGORY]->(:Category)    (:Section)-[:LINKS_OUT]->(:ExternalLink)
                                       (:Section)-[:MENTIONS]->(:Topic)
```

Beyond the plan: `anchor_kind` and `char_count` (so link quality and
over-fragmentation are queryable), `retrievable` (so events can sit in the
graph without polluting results), and a `section_search` fulltext index over
`heading` and `text` — free, and the lexical half of a hybrid retriever.

---

## Adding topics and eval questions

Not yet wired up. When Phase 2 lands, topics come from a hand-maintained
`topics.yaml` (`name` + `aliases`); the LLM may only classify sections *into*
that list and never invent an entry. Eval questions go in
`eval/questions.yaml` as `question` + `expected_urls`.

---

## Phase status

| Phase | State |
|---|---|
| 1 — ingestion via the WP REST API | **done**, verified against the live site |
| 2 — topics and embeddings | not started |
| 3 — hybrid retrieval + `POST /query` | not started |
| 4 — incremental sync and link health | `modified_after` and `content_hash` are in place; no runner |
| 5 — evaluation (Hit@5, MRR) | not started |

### Known gaps

- `/aicr/aicr-gpu-dashboard/` yields no sections: its body is a plugin
  shortcode with no prose. Correctly reported, not silently skipped.
- 101 of 360 sections (28%) are under 200 characters, which is small for
  reliable embedding. Merging them into their parent is proposed, not done.
- 7 internal links are genuinely broken (6 are PDFs under `/files/2025/08/`).

---

## Layout

| Path | What it is |
|---|---|
| `ingest/` | WP API client, sitemap reader, sectioner, anchors, URL rules, orchestrator |
| `graph/` | Neo4j driver, schema (constraints + indexes), batched writer |
| `retrieval/` | Phase 3 — empty |
| `api/` | Phase 3 — empty |
| `eval/` | Phase 5 — empty |
| `scripts/` | Neo4j install / start / stop / dump |
| `tests/` | 49 unit tests: sectioning, anchors, URL normalization |
