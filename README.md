# RC-KG — grounded answers with exact links, from the RC website

Ask a question in plain English; get an answer built **only** from
rc.northeastern.edu content, plus the specific page sections it came from.

Built for the Northeastern Research Computing team, who need to paste a link to
the page that actually supports the answer they give a researcher.

> **Status:** Phases 1–3 and 5 are built and verified against the live site
> (2026-10-22): ingestion, topics and embeddings, GraphRAG retrieval with an
> HTTP API for TicketMate, and a retrieval eval (Hit@5 = 1.00, MRR = 0.87 on
> 22 ticket-style questions). See [Phase status](#phase-status).

---

## The two rules the design exists to enforce

1. **Nothing is stored without a URL.** Enforced in `graph/writer.py`, which
   refuses a `Page` row with no `url` and a `Section` row with no
   `url_with_anchor`.
2. **The model never writes a URL.** It cites `[S1]`-style markers, which
   `retrieval/citations.py` turns into the retrieved section's exact URL. Any
   URL the model writes anyway is checked against what was retrieved and
   stripped if it is not there. An LLM that invents a plausible-looking RC
   URL is worse than one that says "I don't know".

---

## Deploying: build once, serve anywhere

The graph is built once and stored on `/projects`; every job that needs it
loads that copy into its own private Neo4j.

```bash
scripts/build_graph.sh     # ~4 min, needs network to rc.northeastern.edu
scripts/serve_kg.sh --port 8090    # any node that can read /projects
```

**`build_graph.sh`** ingests the live site, embeds, runs the eval, and
publishes to `data/graph/` only if Hit@5 ≥ 0.9 (`--min-hit`, `--force`):

| file | |
|---|---|
| `neo4j.dump` | the whole graph — sections, embeddings, topics, indexes |
| `manifest.json` | build time, git commit, counts, eval scores |
| `ingest_report.json` | what the ingest saw (sitemap diff, broken links, ...) |
| `previous/` | the snapshot it replaced, for rollback |

Publishing is a rename, so a job starting mid-build loads the old snapshot,
and running jobs keep the graph they loaded. It also caches the embedding
model under `vendor/hf`, which is where serving reads it — offline.

**`serve_kg.sh`** makes a fresh `mktemp` directory on node-local disk (Neo4j
cannot run its store on NFS), loads the dump into a Neo4j that lives only
there, and runs the API in the foreground; on exit both stop and the
directory is deleted. Nothing outside it is written, so any number of jobs and
users can run it at once from this checkout. The Neo4j password is generated
per instance and never touches `/projects`. Set `RC_KG_API_TOKEN` to require
a bearer token: loopback is shared by everyone on a compute node. With no
snapshot it falls back to building one from the live site.

**TicketMate starts it automatically**: `ood-apps/rc-copilot/template/script.sh.erb`
and `cluster/serve_stack.sbatch` launch `serve_kg.sh` before vLLM, with a
fresh port and token, and point the backend at it (`RC_KG_URL`,
`RC_KG_API_TOKEN`). It is ready in 1–2 minutes — Neo4j loads the snapshot in
~20 s; the rest is `import transformers` probing optional packages over NFS
(measured 25–58 s) — while vLLM is still loading weights.

To refresh the graph after the website changes, re-run `build_graph.sh`; new
TicketMate sessions pick it up.

## Setup (development)

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
# Phase 1 — ingest. No LLM, no embeddings, ~100 polite requests, ~2 min.
.venv/bin/python -m ingest.run --report data/phase1_report.json

# Phase 2 — topics (from topics.yaml) and embeddings. Local model, no network
# after the first model download. ~50 s on 2 CPUs; re-runs only embed
# sections whose text changed.
.venv/bin/python -m enrich.run

# Phase 3 — try retrieval, or serve it.
.venv/bin/python -m retrieval.query "my job was killed with OUT_OF_MEMORY"
.venv/bin/uvicorn api.app:app --host 127.0.0.1 --port 8090

# Phase 5 — retrieval eval.
.venv/bin/python -m eval.run

.venv/bin/python -m pytest tests/ -q
```

`--clear` on the ingest wipes the graph, embeddings included. It is only
needed when pages have been removed from the site; otherwise everything is
MERGE-on-key and updates in place.

**On Explorer, local HTTP goes through a proxy.** `http_proxy` is set with no
`no_proxy`, and the Squid answers 403 for `127.0.0.1` and node addresses. The
code bypasses it for internal hosts (`retrieval/http.py`); for `curl`, pass
`--noproxy '*'`.

---

## What the graph contains

Built 2026-10-22: 114 items → 380 sections.

| content type | items | sections | retrievable |
|---|---|---|---|
| page | 35 | 221 | 185 |
| post | 55 | 112 | 97 |
| calendar_event | 24 (19 API + 5 sitemap) | 47 | 0 by default |

Graph: `Page=114  Section=380  Topic=30  Category=13  ExternalLink=113`,
`HAS_SECTION=380  NEXT=268  LINKS_TO=251  LINKS_OUT=285  MENTIONS=1171
IN_CATEGORY=77  CHILD_OF=15`. Every section has a 384-dim `bge-small-en-v1.5`
embedding.

51 sections are hidden from retrieval as duplicates: 22 texts repeat across
pages (the "How Can Research Computing Support You?" footer appears 26 times;
staff bios are repeated on every event page). One canonical copy stays
retrievable and the others carry `duplicate_of`.

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
only stale events. This is why the sitemap cross-check is not optional. Each live page
advertises its own REST record (`<link rel="alternate" type="application/json">`),
and fetching that by id works, so `wp_api.fetch_from_page` recovers every
sitemap-only item; they are stored with `source = "sitemap"`.

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
           anchor_kind, content_hash, order, char_count, retrievable,
           duplicate_of, embedding, embedding_model, embedded_key})
(:Topic {name, description, aliases})  (:Category {wp_id, name, taxonomy})
(:ExternalLink {url, domain})

(:Page)-[:HAS_SECTION]->(:Section)     (:Section)-[:NEXT]->(:Section)
(:Page)-[:CHILD_OF]->(:Page)           (:Section)-[:LINKS_TO]->(:Page)
(:Page)-[:IN_CATEGORY]->(:Category)    (:Section)-[:LINKS_OUT]->(:ExternalLink)
                                       (:Section)-[:MENTIONS {hits, in_heading}]->(:Topic)
```

Beyond the plan: `anchor_kind` and `char_count` (so link quality and
over-fragmentation are queryable), `retrievable` (so events can sit in the
graph without polluting results), and a `section_search` fulltext index over
`heading` and `text` — free, and the lexical half of a hybrid retriever.

---

## How retrieval works (GraphRAG)

`retrieval/graphrag.py`, one call per ticket or question:

1. **Seeds.** Vector search (`section_embeddings`) and fulltext search
   (`section_search`) over retrievable sections, fused with reciprocal rank
   fusion. The query is also matched against `topics.yaml`, and sections that
   MENTION those topics get a small boost. Vector bridges paraphrase ("my job
   got killed" → the OOM section); fulltext catches exact tokens
   (`seff`, `gpu-interactive`, an error string). At most 3 sources per page.
2. **Graph expansion,** per seed: neighbouring sections (`NEXT`, both ways),
   linked RC pages (`LINKS_TO`), linked external docs (`LINKS_OUT`, mostly
   rc-docs, where the commands live) and topics.
3. **Output:** numbered sources `[S1]…` with exact URLs and publish dates, a
   `context` block ready to paste into a prompt, `allowed_urls`, and
   `low_confidence` when the best vector match is under 0.78 (on-topic
   tickets score 0.85–0.93; "what's the weather in Boston" scores 0.74).

## Using it from TicketMate (RC Copilot)

TicketMate keeps its own vLLM and prompt; RC-KG supplies the grounding.

```
POST /retrieve  {"query": <ticket text>, "k": 6}
  -> {sources: [{ref, url, page_title, heading, text, published, topics,
                 links, external_links, score, ...}],
      context: "[S1] Partitions > ...\nURL: ...\n...", allowed_urls: [...],
      query_topics: [...], low_confidence: bool}

POST /cite  {"texts": [<reply>, <step>, ...], "refs": {"S1": url, ...},
             "allowed_urls": [...], "style": "numbered"}
  -> {texts, cited, references: [{n, ref, url}], removed_urls, unknown_refs}
     (or "text" in, "text" out; styles: markdown | plain | numbered)

POST /answer   retrieve + LLM + cite, when LLM_BASE_URL is set
GET  /health   "loading" until Neo4j and the embedding model are ready; no token
```

TicketMate (`backend/app/kg_client.py`) puts `context` into its prompt with
an instruction to cite `[S#]` and never write URLs, then sends every field of
the draft through one `/cite` call in `numbered` style, so the reply's `[1]`
and the steps' `[1]` are the same page. The reply gets a References block
above its sign-off; the steps get a Sources line, since they go to work notes
on their own. If rc_kg is down, the draft is produced as before with a caveat
saying it was not checked against the website.

Ticket text never leaves the cluster: embeddings run in-process, and the API
binds `127.0.0.1`.

## Adding topics and eval questions

**Topics** live in `topics.yaml` (`name`, `description`, `aliases`). A section
MENTIONS a topic only when an alias appears in it literally (whole word,
case-insensitive), so every edge is explainable and nothing is invented.
Re-run `python -m enrich.run` after editing; topics removed from the file are
removed from the graph.

**Eval questions** go in `eval/questions.yaml` as `question` + `expected_urls`
(page URLs). Phrase them like tickets, not like headings.

---

## Phase status

| Phase | State |
|---|---|
| 1 — ingestion via the WP REST API | **done**, verified against the live site |
| 2 — topics and embeddings | **done**: 30 topics, 1171 MENTIONS, all 380 sections embedded |
| 3 — GraphRAG retrieval + HTTP API | **done**: `/retrieve`, `/cite`, `/answer`; `/answer` tested against a stub LLM only |
| 4 — incremental sync and link health | `modified_after`, `content_hash` and `embedded_key` are in place; no runner |
| 5 — evaluation (Hit@5, MRR) | **done**: Hit@1 0.77, Hit@5 1.00, MRR 0.87 on 22 questions |

### Known gaps

- `/aicr/aicr-gpu-dashboard/` yields no sections: its body is a plugin
  shortcode with no prose. Correctly reported, not silently skipped.
- 101 of 360 sections (28%) are under 200 characters, which is small for
  reliable embedding. Merging them into their parent is proposed, not done.
- 6 internal links are genuinely broken (5 are PDFs under `/files/2025/08/`).
- `/aicr/rc-internal` also yields no sections (same dashboard plugin).
- The eval set is 22 questions written by one person from the page
  inventory. It shows retrieval works; it is too small to tune weights on.
- Nothing rebuilds the snapshot on a schedule yet: re-run
  `scripts/build_graph.sh` when the site changes (a cron'd sbatch would do).

---

## Layout

| Path | What it is |
|---|---|
| `ingest/` | WP API client, sitemap reader, sectioner, anchors, URL rules, orchestrator |
| `graph/` | Neo4j driver, schema (constraints + indexes), batched writer |
| `topics.yaml` | The closed topic vocabulary |
| `enrich/` | Phase 2 — topic matching and embedding |
| `retrieval/` | Embedding providers, GraphRAG retriever, citation enforcement, CLI |
| `api/` | FastAPI service for TicketMate |
| `eval/` | Eval questions and Hit@k / MRR runner |
| `scripts/` | `build_graph.sh`, `serve_kg.sh` (and their `_kg_instance.sh`); dev Neo4j install / start / stop / dump |
| `tests/` | 63 unit tests: sectioning, anchors, URLs, duplicates, topics, citations |
