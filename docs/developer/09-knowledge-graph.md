---
name: 9. The knowledge graph (rc-kg)
route: /knowledge-graph
menu: Knowledge
---

# 9. The knowledge graph (rc-kg)

rc-kg turns RC's two public sites into a Neo4j graph of page sections with
embeddings, and serves hybrid retrieval, citation rendering and support checks
over HTTP. It has its own Python environment (`rc-kg/.venv`) and its own
reference documents, which this chapter explains and links to:

- `rc-kg/README.md` -- design, measurements, evaluation results, known gaps
- `rc-kg/API_CONTRACT.md` -- every field a client may rely on

The `rc-kg/` directory is identical in TicketMate and rc-agent.

## 9.1 The two rules

The design exists to enforce two rules:

1. **Nothing is stored without a URL.** `graph/writer.py` raises
   (`refusing to write ... Page row(s) with no url`) for a page with no `url`
   or a section with no `url_with_anchor`; `graph/schema.py` also creates
   existence constraints for both (Enterprise-only, so on Community they are
   logged as unavailable and the writer's check is what holds).
2. **The model never writes a URL.** It cites `[S1]` markers, which
   `retrieval/citations.py` turns into the retrieved section's exact URL. Any
   URL the model writes anyway is checked against what was retrieved and
   removed if it is not there (chapter 10).

## 9.2 Sources and ingest

| | RC website | RC documentation |
|---|---|---|
| URL | `rc.northeastern.edu` | `rc-docs.northeastern.edu/en/latest/` |
| covers | policies, services, people, news, events | how-to: jobs, GPUs, software, storage, transfer |
| built with | WordPress | Sphinx on Read the Docs |
| page list | REST API (`WP_API`), cross-checked against the sitemap (`SITEMAP_INDEX`) | Sphinx's `searchindex.js` (the build's own page list) |
| section links | a real `#id` where one exists, else a Scroll-to-Text Fragment `#:~:text=...` | a real `#id` for every heading |
| dates | WordPress published / modified | last commit per page on `master` of the public source repo (one metadata-only clone) |
| code | `ingest/run.py`, `wp_api.py`, `sitemap.py` | `ingest/rtd.py`, called from the same run |

`python -m ingest.run --report <file>` (Phase 1) contains **no LLM and no
embedding call**, so sectioning, anchors and links can be inspected cheaply.
Steps:

1. Apply the schema (9.3); `--clear` wipes the data first (only needed when
   pages were removed from the site; otherwise every write is MERGE-on-key).
2. Confirm the REST endpoint names; fetch every item of each content type
   (`page`, `post`, `calendar_event`) and the categories. Pagination follows
   `X-WP-TotalPages`, not "until an empty page": an empty page is also what a
   transient 5xx behind a cache looks like.
3. **Sitemap cross-check.** The API and the sitemap disagreed completely
   about events; asking the API for a sitemap-only slug returns `[]`. Each
   sitemap-only item is recovered by fetching the REST record its own page
   advertises (`wp_api.fetch_from_page`) and stored with `source = "sitemap"`.
4. List the docs pages, so website links to them become `LINKS_TO` edges
   rather than `ExternalLink`s.
5. Write `Page` nodes, `Category` nodes and `IN_CATEGORY`, `CHILD_OF` (from
   WordPress's parent field).
6. Split each page into sections (`ingest/sections.py`): a new section at
   every H2 or H3; content before the first heading is an `Overview`
   section; H4-H6 stay inside. The document is flattened once into a stream
   of events in document order (recursing into nested block-theme wrappers
   double-counted text in a first version). Tables, lists and code are kept
   as structure; code blocks keep their exact text and line breaks, fenced
   with ```` ``` ````.
7. Resolve each heading to a link (`ingest/anchors.py`): a real `id` if the
   heading has one (`anchor_kind = "id"`), else a text fragment
   (`"text_fragment"`; works in Chromium and Safari 16.1+, **not Firefox**),
   else `"none"`.
8. Resolve links (`ingest/urls.py`). URL normalisation is the join key:
   lowercase scheme and host, drop `www.`, force https, drop query and
   fragment, collapse `//`, strip the trailing slash. Internal redirects are
   followed (`RESOLVE_REDIRECTS=true`; a third of internal hrefs are
   redirects). Links to any docs version map to the same page under `latest`.
9. Mark what retrieval must not return (sections stay in the graph; only
   `retrievable` changes):
   - **calendar events** -- `retrievable = false` unless
     `INCLUDE_EVENTS_IN_RETRIEVAL=true`;
   - **duplicates** (`_mark_duplicates`) -- sections with the same
     `content_hash`; one canonical copy stays retrievable (docs first, then
     pages, posts, events), the rest get `duplicate_of`;
   - **people, not support** (`_mark_not_support`) -- the team and AVP pages,
     posts in the Researcher Spotlight and From the AVP categories, and bios
     repeated elsewhere: they ranked in the top 6 whenever a ticket named a
     person or carried a signature;
   - **feed pages** (`feed_lead`) -- a section whose bullets begin with the
     titles of at least 3 posts already in the graph keeps only its lead
     paragraph; each post is retrieved on its own, with its date.
10. Write the report (`Report`: counts per type, anchor kinds, sitemap diff,
    recovered items, docs stats, duplicates, not-retrievable, redirects,
    broken links, external domains, graph counts, warnings).

HTTP is one polite session (`ingest/http.py`): `REQUEST_DELAY=1.0` s between
requests, a `USER_AGENT` naming the project.

Section ids: `<content_type>-<wp_id>-<order:03d>` for WordPress
(`ingest.run.section_id`), `rtd.section_id(docname, order)` for docs.

## 9.3 Graph schema

```
(:Page {site, content_type, title, url, published, last_modified, slug, wp_id, source})
        site: rc.northeastern.edu | rc-docs.northeastern.edu
        content_type: page | post | calendar_event | doc
(:Section {section_id, heading, heading_level, text, url_with_anchor, anchor_kind,
           content_hash, order, char_count, retrievable, duplicate_of,
           embedding, embedding_model, embedded_key})
(:Topic {name, description, aliases})
(:Category {wp_id, name, taxonomy})
(:ExternalLink {url, domain})

(:Page)-[:HAS_SECTION]->(:Section)        (:Section)-[:NEXT]->(:Section)
(:Page)-[:CHILD_OF]->(:Page)              (:Section)-[:LINKS_TO]->(:Page)
(:Page)-[:IN_CATEGORY]->(:Category)       (:Section)-[:LINKS_OUT]->(:ExternalLink)
                                          (:Section)-[:MENTIONS {hits, in_heading}]->(:Topic)
```

`graph/schema.py` creates, idempotently on every ingest and enrich:

| kind | name | on |
|---|---|---|
| unique | `page_url` | `Page.url` (stops a trailing-slash difference making two nodes) |
| node key | `page_wpid_type` | `(Page.wp_id, Page.content_type)` (ids are unique only within a type) |
| unique | `section_id`, `topic_name`, `category_wpid`, `extlink_url` | |
| existence | `page_url_exists`, `section_url_exists` | Enterprise only; tolerated if refused |
| index | `page_content_type`, `page_published`, `section_hash`, `section_retrievable` | |
| fulltext | `topic_search` | `Topic.name`, `Topic.aliases` |
| fulltext | `section_search` | `Section.heading`, `Section.text` -- the lexical half of retrieval |
| vector | `section_embeddings` | `Section.embedding`, `EMBEDDING_DIM` (384) dimensions, cosine |

`rc-kg/README.md` (*What the graph contains*) has the node and edge counts
of the published graph.

## 9.4 Enrichment: topics and embeddings

`python -m enrich.run` (Phase 2) reads sections from Neo4j -- it never
re-fetches the site -- so it can be re-run whenever `topics.yaml` or the
model changes.

**Topics.** `topics.yaml` is a closed, hand-maintained vocabulary: `name`,
`description`, `aliases`. A section MENTIONS a topic only when an alias (or
the name) appears in its heading or text literally -- whole word,
case-insensitive, longest alias first, with word boundaries spelled out so
`/` and `.` work (`enrich/topics.py`). Every edge can be explained by pointing
at the words. Each run upserts the topics, **deletes topics no longer in the
file**, clears every MENTIONS edge and rewrites them with `hits` (alias
matches) and `in_heading`.

**Embeddings.** `retrieval/embeddings.py`, chosen by `EMBEDDING_PROVIDER`:

| provider | runs |
|---|---|
| `sentence_transformers` (default) | in-process, CPU or GPU, batches of 32, normalised vectors. Default because ticket text must not leave the university |
| `openai`, `vllm`, `openai_compatible` | an OpenAI-compatible `/embeddings` at `EMBEDDING_BASE_URL`, batches of 64 |

| setting | default |
|---|---|
| `EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` |
| `EMBEDDING_DIM` | 384 (the loader refuses a model with another size) |

- What is embedded is the section **in its page's context**:
  `"<page title> > <heading>\n<text>"` (`enrich.run.embedding_text`); a
  section headed "Solution:" means nothing alone.
- BGE models get the query prefix `Represent this sentence for searching
  relevant passages: ` on **queries only**.
- Incremental: `embedded_key = "<model>:<content_hash>"`; only sections whose
  text or model changed are re-embedded (`--reembed` forces all). Vectors are
  written with `db.create.setNodeVectorProperty`, in batches of 64.
- The model name is stored on every section and **checked at query time**:
  retrieval raises if the graph was embedded with another model (mixing
  models silently ruins retrieval).

## 9.5 Hybrid retrieval

`retrieval/graphrag.py`, `retrieve(query, k=6, include_events=None,
candidates=30, today=None)`. Every number below is a module constant whose
comment records the measurement that chose it.

### Seeds: vector + fulltext, fused

1. **Vector**: `db.index.vector.queryNodes('section_embeddings', 30, vec)`
   over retrievable sections (plus, with events on, non-duplicate event
   sections).
2. **Fulltext**: `db.index.fulltext.queryNodes('section_search', q, {limit: 30})`.
   The query (`_fulltext_query`) is a Lucene OR of the ticket's distinctive
   tokens: tokens under 3 characters, stopwords, pure numbers and repeats
   dropped; Lucene specials escaped; at most `_MAX_FULLTEXT_TERMS = 40`.
   Alphanumeric tokens of 4+ characters also get a prefix form
   (`partition OR partition*`): the index does not stem, and the site writes
   compounds as one word (`Jupyter` -> `JupyterLab`).
3. **Reciprocal rank fusion**: each retriever adds `weight / (60 + rank)`
   (`_RRF_K = 60`, the original paper's value; `_FULLTEXT_WEIGHT = 1.0`).
4. **Topic boost**: the query is matched against `topics.yaml`. Sections that
   MENTION those topics (weight `hits`, x3 when in the heading) add a third
   RRF term, **only to sections the first two already found**
   (`_TOPIC_INTRODUCES = False`: letting topics introduce sections pulled the
   same long pages into every storage ticket). With `_TOPIC_MODE = "idf"` the
   term's weight is `0.3 x` the IDF of the query's most specific topic
   (`idf = log(N/df) / log(N)` over retrievable sections), so "Explorer
   cluster", which nearly every ticket and section mentions, barely moves the
   ranking.

### Re-ranking

5. The top `k x 4` candidates are expanded (one `EXPAND` query: page
   metadata, previous and next sections, topics, categories, links).
6. **Stale status posts**: a `post` in a status category (`Status Updates`,
   `Explorer Status`, `Discovery Status`, `AICR`) or with the topic
   `Maintenance and outages`, published more than **30 days** ago
   (`_STALE_AFTER_DAYS`), has its score multiplied by **0.6**
   (`_STALE_FACTOR`) -- unless the query itself matches that topic. Down-weighted,
   not dropped; 0.6 was the best MRR in a sweep of 0.4-0.7.
7. **At most 3 sources per page** (`_MAX_PER_PAGE`), until `k` sources.

### Graph expansion and output

Each source carries its neighbours and links, from the graph:

| field | from |
|---|---|
| `context_before`, `context_after` | previous / next section via `NEXT` (400 characters each) |
| `topics` | `MENTIONS` |
| `links` | site pages via `LINKS_TO` |
| `external_links` | `LINKS_OUT` (mostly rc-docs) |
| `score`, `vector_score`, `signals` | fused score, cosine, rank per retriever (`stale: 1` when down-weighted) |

`format_context` builds the prompt block, sources separated by
`\n\n---\n\n`:

```text
[S1] Page title > Heading
URL: <url_with_anchor>
Source: RC documentation (rc-docs), last updated 2026-10-01     (docs only)
Published: 2026-09-15                                           (not for `page`)
Topics: Slurm, Memory
<section text, at most 2,500 characters>
Linked documentation: <up to 8 external links>
Related RC pages: Title <url>; ...                              (up to 6)
```

`allowed_urls` = every source's URL, page URL, linked pages and external
links: the only URLs a draft may contain.

### Low confidence

`low_confidence` is true when either:

- the best vector score is below **`LOW_CONFIDENCE_BELOW = 0.83`**. Measured
  2026-10-26 on short queries (subject, description, latest message) with
  bge-small on the full graph: the 45 eval questions score 0.840-0.93,
  off-topic questions (parking, payroll, ...) at most 0.801. The earlier
  0.78 had been set on whole-ticket queries, which score high whatever they
  say -- one reason TicketMate sends the short query, not the thread; or
- **`known_gap(query)`** recognises a problem no page covers, by what the
  query says: a node name (`[a-z]{1,2}\d{4}`) with a fault word (down,
  drained, not responding, `ReqNodeNotAvail`, ...) -> "a node fault"; a
  slowness word with a cluster word -> "cluster slowness". Such tickets
  scored 0.833-0.882, inside the range of covered ones, so no threshold could
  catch them.

The `trace` returned with every result: `timings_ms` (`embed`, `vector`,
`fulltext`, `topics`, `expand`, `total`), `candidates` (counts, including
`stale_status_posts`), `settings` (every constant above and the embedding
model), `best_vector_score`, `low_confidence_threshold`, `known_gap`,
`context_chars`. No query text.

Try it from the command line:

```bash
cd rc-kg && scripts/with_kg.sh -m retrieval.query "my job was killed with OUT_OF_MEMORY"
```

## 9.6 The HTTP API

`api/app.py`, FastAPI, `API_VERSION = "1.5"`. Started by `serve_kg.sh` on
`127.0.0.1` only.

| endpoint | token | ready | does |
|---|---|---|---|
| `GET /health` | no | no | `loading`/`failed` until warm-up is done, then `ok` with `sections`, `embedded`, `retrievable`, `embedding_model`, `snapshot`, `llm_configured`, `swaps` (last 5). Always 200 |
| `POST /retrieve` | yes | yes | `{query (<= 50,000 chars), k 1-20 = 6, include_events}` -> the retrieval result as JSON, its `trace` plus `request_id`, `api_version`, `snapshot`; 400 for an empty query |
| `POST /cite` | yes | no | exactly one of `text`/`texts`, plus `refs` (`{"S1": url}`), `allowed_urls`, `style` (`markdown`, `plain`, `numbered`), `check_support`. Returns the rendered text(s), `cited`, `references`, `removed_urls`, `unknown_refs`, `support` (only with `check_support` and once ready), `trace`. 422 unless exactly one of `text`/`texts` |
| `POST /similarity` | yes | yes | `{query, texts (1-20)}` -> cosine of query (embedded as a query) to each text (as a passage, cut to 20,000 chars), with the retrieval model |
| `POST /answer` | yes | yes | retrieve + an LLM at `LLM_BASE_URL` + cite; 503 without one. Not used by TicketMate |
| `POST /admin/swap` | yes | no | switch to a newer graph (9.8) |

- **Token.** With `RC_KG_API_TOKEN` set, every endpoint but `/health` needs
  `Authorization: Bearer <token>`, compared with `hmac.compare_digest`; 401
  otherwise. Loopback is shared by every user on a node, so the job generates
  a token and gives it to both sides.
- **Ready.** `require_ready` returns 503 ("still loading", or the warm-up
  error) until warm-up finishes.
- **Warm-up** (`_warm`, a background thread started by the lifespan hook, so
  the port opens at once): load the embedding model; wait for Neo4j to answer
  with embedded sections (up to `RC_KG_WARM_TIMEOUT`, default 900 s); run
  five varied throwaway queries. Neo4j's JVM optimises each query over its
  first executions: without warm-up the first ticket took 3.1 s, with one
  query 0.96 s, with five about 0.13 s.
- **Logging.** One line per request on the `rc_kg.requests` logger: the
  `X-Request-ID` the client sent, counts and timings, **never the query**.
- **Versioning.** Clients check the major number. MAJOR goes up when an
  existing client could break, MINOR for additions; `API_CONTRACT.md` has the
  change log. `scripts/contract_test.sh` asserts every contract field against
  a fresh instance (`eval/contract_check.py`); TicketMate's
  `tests/rc_kg/run_contract.sh` checks the client side (grounding, citation
  numbering, a v2 server refused, rc-kg down, the website-only variant, a
  model without `json_schema`).

The client side is `backend/app/kg_client.py`:

| behaviour | value |
|---|---|
| compatibility | `EXPECTED_API_MAJOR = "1"`; `/health` (3 s timeout) checked until compatible, then cached for the process. Another major means drafts are ungrounded, with a caveat naming the versions |
| `/retrieve` | `{"query": prepared.query, "k": RC_KG_K}` (default 6), timeout `RC_KG_TIMEOUT_SECONDS` (15 s) |
| transport | `urllib` on a worker thread with an empty `ProxyHandler` (the site proxy 403s loopback); sends `X-Request-ID` and the bearer token |
| failure | never raises: a `KGContext` with status `unavailable` (or `disabled` without `RC_KG_URL`), and the draft carries a caveat |
| trace | an `rc_kg.retrieve` run (type `retriever`) with the documents, plus derived child runs `rc_kg.embed_query`, `vector_search`, `fulltext_search`, `topic_boost`, `graph_expand` built from rc-kg's stage timings |

> **rc-agent:** `kg_client.retrieve_multi` runs a second search when
> `ticket_context` derived documentation vocabulary from the ticket's
> symptoms (the subject plus that expansion), and `kg_client.merge` appends
> its new pages after the first search's, renumbered, at most 8 sources. The
> first search decides `low_confidence`. Agents can also search again with
> their own words (a tool). Chapter 16.

## 9.7 Snapshots and the shared data store

A graph is built once and published as a **snapshot**; every session loads
a copy into its own private Neo4j.

```
$RC_KG_DATA_ROOT/                     default /projects/rc/projects/rc-kg-data
  graph/                              the full graph: website + rc-docs
    neo4j.dump                        the whole database: sections, embeddings, topics, indexes
    manifest.json                     built_at, git_commit, variant, sites, embedding model and dim,
                                      neo4j_version, counts, eval, ragas, graph_integrity
    ingest_report.json  retrieval_eval.json  graph_eval.json
    previous/                         the snapshot this one replaced (hard links), for rollback
  graph-website/                      the same for the website-only variant
  .build-<variant>.lock/              while a rebuild runs (9.8)
```

`_kg_instance.sh` (`kg_data_root`) chooses the root: `$RC_KG_DATA_ROOT` if
set, else `/projects/rc/projects/rc-kg-data` if it exists, else the
checkout's own `rc-kg/data/`. TicketMate and rc-agent therefore read and
publish **the same graph**. `RC_KG_SNAPSHOT` overrides the directory for one
command.

| variant | contents | directory | eval gate on |
|---|---|---|---|
| `full` (default) | RC website + rc-docs | `graph/` | all eval questions |
| `website` | RC website only (`DOCS_ENABLED=false`) | `graph-website/` | questions the website can answer |

The README's *Graph variants* section has the comparison that keeps `full`
the default. A session picks one with `RC_KG_VARIANT`; `serve_kg.sh` refuses
a snapshot whose manifest names a different variant, and `/health` reports
`snapshot.variant`.

### Building: `scripts/build_graph.sh`

```bash
rc-kg/scripts/build_graph.sh                     # full
rc-kg/scripts/build_graph.sh --variant website
rc-kg/scripts/build_graph.sh --all               # both, each in its own process
rc-kg/scripts/build_graph.sh --min-hit 0.8       # loosen the gate (default 0.9)
rc-kg/scripts/build_graph.sh --force             # publish even below the gate
```

About 4 minutes and ~100 requests; needs network to the sites. In a private
Neo4j on node-local disk, deleted afterwards:

1. make sure the embedding model is cached under `vendor/hf` (offline first;
   a compute node reaches the Hub only through a flaky proxy);
2. `ingest.run`, then `enrich.run`;
3. **gate**: `eval.run --min-hit 0.9` (Hit@5 on `eval/questions.yaml`, for
   the variant's sites);
4. `eval.retrieval_eval` (RAGAS context metrics, kept beside the snapshot);
5. `eval.graph_eval` integrity checks -- a **hard gate**, whatever `--force`
   says;
6. write `manifest.json`; stop Neo4j; `neo4j-admin database dump`;
7. **publish atomically**: copy to `<dir>/.staging.<pid>` on the same
   filesystem, `chmod -R a+rX` (other users' sessions load it), hard-link the
   current files into `previous/`, then `mv` each file over the old one. A
   job starting mid-publish loads the old dump;
8. refresh the serving runtime archive (`build_runtime.sh`).

### Serving: `scripts/serve_kg.sh`

```bash
rc-kg/scripts/serve_kg.sh --port 8090 [--variant website]
```

The launchers start it before vLLM with a fresh port and token, and point the
backend at it (`RC_KG_URL`, `RC_KG_API_TOKEN`).

1. Write the startup checklist (with `RC_KG_PROGRESS_DIR`: one
   `<step>.json` per step -- `prepare`, `runtime`, `neo4j_load`,
   `neo4j_start`, `api_start`, `embedder`, `graph`, `warmup` -- that the
   backend's `/api/health` reports while the session loads).
2. Check the snapshot's variant; sweep this user's stale instance
   directories.
3. Create an instance directory `rc_kg_<user>_<job>_XXXX` under
   `RC_KG_TMP`/`$TMPDIR`/`/tmp` (node-local: Neo4j does not support its store
   on NFS), mode 700, with its own `neo4j.conf`: Bolt on a free loopback port,
   **HTTP disabled**, auth on, heap 512m-1g, page cache 256m. A fresh random
   password, only in the environment of this job's processes.
4. Start a watchdog: if the script is killed outright, it stops Neo4j and
   the API and removes the directory. The next start on the node sweeps
   anything that survived even that (`kg_ps.sh` lists instances).
5. Unpack the runtime archive to local disk once per node (importing the
   Python stack from `/projects` cost 85-110 s of NFS metadata calls).
6. Start the API first (its imports overlap Neo4j's load), then
   `neo4j-admin database load` the dump and start Neo4j. Neo4j 5.26 needs
   Java 17+; `kg_pick_java` chooses one explicitly, because on Java 11 both
   commands print "Unsupported Java" and exit 0.
7. Start `refresh_kg.sh` in the background (9.8).

With no snapshot it builds one from the live sites (~4 min). On exit
everything is stopped and the directory removed; if startup failed, Neo4j's
logs are first copied to `<progress dir>/neo4j-logs/`.

`scripts/with_kg.sh -m <module>` runs any module against a private copy of a
snapshot, without the API (how every eval runs).

## 9.8 Keeping the graph current while serving

A session never waits for the website: it serves the published snapshot at
once. `scripts/refresh_kg.sh <port> <variant> <instance dir> <built_at>`
then runs in the background for the whole session. First check after
`RC_KG_REFRESH_FIRST_MIN` (2) minutes, then every `RC_KG_REFRESH_CHECK_MIN`:

1. **A newer snapshot is published** (its `built_at` is later than the one
   being served -- built by any session of either app, or by
   `rebuild.sbatch`): load it into a **second** private Neo4j, then
   `POST /admin/swap`. On success the Neo4j it replaced is stopped.
2. **Otherwise, the published snapshot is older than
   `RC_KG_REFRESH_MAX_AGE_H`** and `RC_KG_REFRESH` is on: take the build
   lock, run `build_graph.sh --variant <v>` under `nice -n 10`, release the
   lock, then switch as in 1. A build that fails the gate publishes nothing;
   the old graph keeps serving.

Nothing in it can take the session's graph away: every failure is logged and
the current graph keeps serving.

### `POST /admin/swap`

Body: `{"uri": "bolt://127.0.0.1:<port>", "user": "neo4j", "password": ...,
"manifest": "<snapshot>/manifest.json"}`. Bearer token required.

- The URI must match `bolt://127.0.0.1:<port>`; anything else is 400.
- `graph/connection.swap` opens a new driver, verifies connectivity and
  counts embedded sections; zero, or any error, is **409** and the old driver
  keeps serving.
- Under a lock, the new driver replaces the old one for every later query;
  `connection.current` records the new URI and manifest, so `/health` reports
  the snapshot actually served.
- The old driver is closed after **`SWAP_GRACE_S = 60`** seconds, so queries
  in flight finish. (The README records 45 queries during a switch, none
  failed.)
- The swap is appended to `swaps` in `/health` and logged.

### The build lock

`$RC_KG_DATA_ROOT/.build-<variant>.lock` is a directory, taken with
`mkdir` (atomic): one build per variant at a time, across every session and
both apps. Its `owner` file says `<host> pid <pid> user <user> job <job>
since <time>`. When the lock is held:

| holder | action |
|---|---|
| this node, process gone (a killed build) | removed at once, then taken |
| younger than 4 hours (`LOCK_STALE_S`) | wait; switch when it publishes |
| older than 4 hours | treated as stale, removed and taken |

### Settings

| variable | default | |
|---|---|---|
| `RC_KG_REFRESH_LOOP` | `on` | `off`: serve the starting snapshot only (tests set this) |
| `RC_KG_REFRESH` | `on` | `off`: never rebuild; still switch to newer snapshots |
| `RC_KG_REFRESH_MAX_AGE_H` | `24` | rebuild when the published snapshot is older |
| `RC_KG_REFRESH_CHECK_MIN` | `20` | minutes between checks |
| `RC_KG_REFRESH_FIRST_MIN` | `2` | the first check, after startup has settled |
| `RC_KG_REFRESH_LOG` | `<progress dir>/refresh.log` | the build's own output goes to `<log>.build` |
| `RC_KG_DATA_ROOT` | `/projects/rc/projects/rc-kg-data` | where snapshots are read and published |

The refresh loop starts only when the served snapshot has a `manifest.json`.

A scheduled rebuild that does not depend on sessions: `scripts/rebuild.sbatch`
(CPU, `short` partition, `build_graph.sh --all`), submitted weekly from
`scrontab`; its header has the line to add.

## 9.9 Configuration

`rc-kg/config.py` (environment, then `.env`, then defaults). The serving
scripts run from the instance directory, so a developer's `rc-kg/.env` never
reaches a session.

| setting | default | |
|---|---|---|
| `WP_API`, `SITEMAP_INDEX` | the RC website's REST API and sitemap | |
| `REQUEST_DELAY`, `REQUEST_TIMEOUT` | 1.0 s, 30 s | crawl politeness |
| `DOCS_ENABLED`, `DOCS_BASE`, `DOCS_REPO`, `DOCS_BRANCH`, `DOCS_SOURCE_DIR` | `true`, `.../en/latest/`, the public docs repo, `master`, `docs/source` | |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD`, `NEO4J_DATABASE` | `bolt://127.0.0.1:7687`, `neo4j`, empty, `neo4j` | set by the scripts per instance |
| `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `EMBEDDING_DIM`, `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY` | `sentence_transformers`, `BAAI/bge-small-en-v1.5`, 384 | |
| `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_TEMPERATURE`, `LLM_MAX_TOKENS` | unset, `Qwen/Qwen2.5-7B-Instruct`, -, 0.1, 1200 | only for `/answer` |
| `INCLUDE_EVENTS_IN_RETRIEVAL` | `false` | |
| `RESOLVE_REDIRECTS` | `true` | |
| `RC_KG_API_TOKEN` | unset | bearer token |
| `RC_KG_MANIFEST` | set by `serve_kg.sh` | the starting snapshot's manifest, for `/health` |

Script-only variables: `RC_KG_VARIANT`, `RC_KG_SNAPSHOT`, `RC_KG_TMP`,
`RC_KG_THREADS` (query-embedding threads, default 2, so torch does not take
vLLM's cores), `RC_KG_PROGRESS_DIR`, `RC_KG_JAVA_HOME`, `RC_KG_NEO4J_WAIT`
(default 300 s), `RC_KG_WARM_TIMEOUT`, and the refresh settings above.

## 9.10 Evaluation

All evals run against a private copy of a snapshot through
`scripts/with_kg.sh`. The README's *Evaluation framework* has the current
scores.

| module | answers | used by |
|---|---|---|
| `eval/run.py` | Hit@1, Hit@k (default 5), MRR over `eval/questions.yaml`, page-level; `--min-hit` exits 1 below the gate | the build gate |
| `eval/retrieval_eval.py` + `metrics.py` | RAGAS context precision / ID precision / ID recall and nDCG, from labels (no LLM), with 95% bootstrap intervals | the build (report) |
| `eval/graph_eval.py` | integrity invariants over every relation (hard gate); `--live` checks links and anchors on the live sites; usefulness, topics, topology | the build (gate) |
| `eval/ticket_cases.py` | regressions from reviewed tickets (`ticket_cases.yaml`): `must_not` pages, `expected` pages, `low_confidence: true` | after a retrieval change |
| `eval/variants.py` | `full` vs `website` on the same questions, cases and recorded tickets | choosing the default |
| `eval/citation_check.py` | the support check: false flags on correct citations, catches on wrong ones | after a retrieval or support change |
| `eval/citation_review.py`, `draft_batch.py` | citations in real drafts from run logs, to a CSV for hand labels (mode 600) | |
| `eval/ragas_llm.py` | LLM-judged RAGAS (own `.venv-eval`, judge must be a university vLLM) | |
| `eval/optimize.py` | sweeps the retriever's constants on questions, regressions and TicketMate's ground truth; a setting that fails a regression is disqualified | tuning |
| `eval/contract_check.py` | a live instance against `API_CONTRACT.md` | `contract_test.sh` |

Unit tests: `rc-kg/.venv/bin/python -m pytest rc-kg/tests -q`.

## 9.11 Viewing the graph in Neo4j Browser

A session's Neo4j cannot be browsed: its config has `server.http.enabled=false`
and it lives in a private directory with a random password. To explore the
graph, load a published snapshot into the **development Neo4j**, which has
the HTTP browser on loopback:

```bash
cd /projects/rc/projects/rc-agent/rc-kg       # or the ticketmate checkout
scripts/install_neo4j.sh                       # once: Neo4j 5.26 into vendor/
scripts/start_neo4j.sh                         # creates /tmp/rc_kg_neo4j, picks ports, writes .env
scripts/stop_neo4j.sh
vendor/neo4j-community-5.26.0/bin/neo4j-admin database load neo4j \
    --from-path=/projects/rc/projects/rc-kg-data/graph --overwrite-destination=true
scripts/start_neo4j.sh                         # reuses the store and its password
```

`start_neo4j.sh` and `neo4j-admin` use the `java` on `PATH`, which must be 17
or newer (unlike the serving scripts, they do not pick a JDK themselves; set
`JAVA_HOME`/`PATH` first if `java -version` says 11). The load replaces only
the `neo4j` database, so the password set by the first start still works.

`start_neo4j.sh` prints the Bolt and browser ports and an `ssh -L` hint; the
password is in `/tmp/rc_kg_neo4j/password` (also `NEO4J_PASSWORD` in
`rc-kg/.env`, mode 600). Both ports bind `127.0.0.1` only, so from your
laptop forward **both** (the browser page connects to Bolt from your
machine):

```bash
ssh -L <http_port>:127.0.0.1:<http_port> -L <bolt_port>:127.0.0.1:<bolt_port> <node>
# then open http://localhost:<http_port>, connect to bolt://localhost:<bolt_port> as neo4j
```

Some queries to start with:

```cypher
// one page and its sections, in reading order
MATCH (p:Page {content_type: 'doc'})-[:HAS_SECTION]->(s) RETURN p, s LIMIT 25;
// what a section links to
MATCH (s:Section)-[r:LINKS_TO|LINKS_OUT|MENTIONS]->(x)
WHERE s.heading CONTAINS 'Memory' RETURN s, r, x LIMIT 50;
// sections hidden from retrieval, and why
MATCH (s:Section) WHERE NOT s.retrievable
RETURN s.duplicate_of IS NOT NULL AS duplicate, count(*) ;
// the fulltext index as retrieval uses it
CALL db.index.fulltext.queryNodes('section_search', 'sbatch OR sbatch*') YIELD node, score
RETURN node.heading, score LIMIT 10;
```

The store is on node-local `/tmp`, so it does not survive the node.
`scripts/dump_neo4j.sh [dir]` keeps a copy. On a laptop with a container
runtime, `NEO4J_PASSWORD=... docker compose up -d` (`rc-kg/docker-compose.yml`)
gives the same Neo4j 5.26 Community.
