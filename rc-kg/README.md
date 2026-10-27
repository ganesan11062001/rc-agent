# RC-KG — grounded answers with exact links, from the RC website and docs

Ask a question in plain English; get an answer built **only** from
rc.northeastern.edu and rc-docs.northeastern.edu content, plus the specific
page sections it came from.

Built for the Northeastern Research Computing team, who need to paste a link to
the page that actually supports the answer they give a researcher.

> **Status (2026-10-26):** in daily use by TicketMate. The published `full`
> graph — RC website + rc-docs, 170 pages, 597 sections — scores Hit@5 0.978,
> MRR 0.795 on 45 ticket-style questions and passes all 9 reviewed-ticket
> cases; real Qwen2.5-7B drafts are grounded in it. Phases 1–3 and 5 are done;
> see [Phase status](#phase-status).

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

   Beyond the URL itself, `retrieval/support.py` checks that each `[S#]` is
   attached to a sentence its page actually says, and flags (never rewrites)
   citations where another retrieved page fits clearly better.

---

## Graph variants

Two graphs are built and published separately, each with its own eval gate
and rollback copy, and a session chooses one:

| variant | contents | snapshot | gated on |
|---|---|---|---|
| `full` (default) | RC website + rc-docs | `data/graph/` | all eval questions |
| `website` | RC website only | `data/graph-website/` | questions the website can answer |

```bash
scripts/build_graph.sh --all                    # both, one after the other
scripts/build_graph.sh --variant website        # one
scripts/serve_kg.sh --port 8090 --variant website
RC_KG_VARIANT=website scripts/with_kg.sh -m eval.run --sites rc.northeastern.edu
```

`serve_kg.sh` refuses a snapshot whose manifest names a different variant,
and `/health` reports `snapshot.variant` (API 1.2), so a session can always
tell which graph it is grounded in. In TicketMate: `RC_KG_VARIANT=website`
(e.g. `sbatch --export=ALL,RC_KG_VARIANT=website serve_stack.sbatch`), and
`/api/health` names the graph.

**Which to run: the evidence** (`eval/variants.py`, 2026-10-26, both graphs
on the same 45 questions, k=6 as TicketMate uses):

| | website only | website + rc-docs |
|---|---|---|
| all 45 questions: Hit@1 / Hit@6 / MRR | 0.49 / 0.69 / 0.57 | **0.69 / 0.98 / 0.80** |
| answer on the website (24) | **0.75** / 1.00 / **0.86** | 0.71 / 1.00 / 0.83 |
| answer only in rc-docs (14) | 0 / 0 / 0 — and only 2 of 14 flagged low-confidence | **0.57 / 1.00 / 0.71** |
| answer on both sites (7) | 0.57 / **1.00** / 0.72 | **0.86** / 0.86 / **0.86** |
| reviewed ticket cases | 9/9 | 9/9 |
| 24 real tickets: sources from rc-docs, prompt chars | 0 of 6, ~9.4k | 1.5 of 6, ~9.4k |

`full` stays the default. A third of the questions are answered only by
rc-docs, and on those the website graph does not just miss: 12 of 14 times it
returns website pages confidently, which is worse than saying nothing.
Adding rc-docs costs a little at the top of website-only answers (3 questions
one rank lower, 2 one higher) and one question ("add a student to my
/projects directory") falls out of the top 6. With 45 questions one question
is 2 points of Hit@k, so only the docs-only gap is large enough to be sure of.
Re-run after a retrieval change:

```bash
for v in full website; do RC_KG_VARIANT=$v scripts/with_kg.sh -m eval.variants collect /tmp/$v.json \
    --runs /scratch/$USER/rc_copilot_runs; done
.venv/bin/python -m eval.variants compare /tmp/website.json /tmp/full.json
```

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

Measured with three instances on one node: ~1.2 GB RAM each (Neo4j 660 MB,
API 575 MB), 8 MB of local disk, retrieval 0.14 s median per query.

**Crashes do not leave debris.** Each instance dir records its owner (pid and
process start time). A watchdog stops Neo4j and the API and removes the dir
if `serve_kg.sh` is killed outright; if the watchdog dies too (the job's
whole cgroup killed), the next start on that node sweeps the user's stale
dirs. `scripts/kg_ps.sh` lists instances as live/STALE; `--clean` removes the
stale ones. Live instances and other users' dirs are never touched.

**Startup is ~35 s** (63 s the first time on a node). Importing the Python
stack straight from `/projects` took 85–110 s, nearly all of it ~55,000 NFS
metadata calls; `scripts/build_runtime.sh` packs site-packages into one
archive that serving unpacks to local disk once per node, and the API's
imports overlap Neo4j's load. What remains is Neo4j itself (~26 s, three JVM
starts). Without a current archive, serving falls back to the venv — slower,
never wrong. Before reporting ready it runs five varied warm-up queries:
Neo4j's JVM optimises each query over its first executions, and without this
every session's first ticket took ~3 s to retrieve instead of ~0.15 s.

**Startup progress.** With `RC_KG_PROGRESS_DIR` set, each startup step writes
`<dir>/<step>.json` (`{"step","status","detail","at"}`; the status is pending,
running, done or failed). The steps are:

1. `prepare`
2. `runtime`
3. `neo4j_load`
4. `neo4j_start`
5. `api_start`
6. `embedder`
7. `graph`
8. `warmup`

The checklist exists from the script's first second, and a fatal error marks
the step that was running as failed, with the reason. TicketMate sets the
directory per session and reports it in its `/api/health`, so the extension can
show progress while the graph loads. This is a file side channel, not part of
the HTTP API. If startup fails, Neo4j's own logs and config are copied into
`<dir>/neo4j-logs/` before the instance directory is removed (never on a normal
stop); `rc_kg.log` names the Java used and how long the snapshot load took.

**TicketMate starts it automatically**: `ood-apps/rc-copilot/template/script.sh.erb`
and `cluster/serve_stack.sbatch` launch `serve_kg.sh` before vLLM, with a
fresh port and token, and point the backend at it (`RC_KG_URL`,
`RC_KG_API_TOKEN`). It is ready well before vLLM finishes loading weights;
until then drafts are produced ungrounded and say so.

To refresh on a schedule, submit `scripts/rebuild.sbatch` from `scrontab`
(weekly, CPU only; instructions in its header). To refresh by hand, re-run
`build_graph.sh`; new
TicketMate sessions pick it up. Running sessions keep the graph they loaded
(no reload, no dropped connections), so a session can be at most one build
out of date.

`scripts/with_kg.sh` runs any module against a private copy of the snapshot,
e.g. `scripts/with_kg.sh -m eval.run`.

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
(`EMBEDDING_PROVIDER`; the LLM is any OpenAI-compatible `LLM_BASE_URL`). Nothing in the code names a vendor.

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

## Two sources, one graph

| | RC website | RC documentation |
|---|---|---|
| URL | rc.northeastern.edu | rc-docs.northeastern.edu/en/latest/ |
| What it covers | policies, services, people, news, events | how-to: jobs, GPUs, software, storage, transfer |
| Built with | WordPress — REST API + sitemap | Sphinx on Read the Docs — `searchindex.js` page list |
| Section links | real `#id` for 6%, text fragments otherwise | real `#id` for every heading |
| Dates | WordPress published/modified | last commit per page in the public source repo |
| Ingest | `ingest/run.py` | `ingest/rtd.py`, called from the same run |

**Why `latest` and not `explorer-main`:** the docs publish both. On
2026-10-22 `explorer-main` was 17 commits behind `master` (which `latest` is
built from), missing the updated GPU table, the new MGHPCC shutdown date, the
data-transfer guide and the storage-query page. Links to any version —
including the RC website's 40 links to `explorer-main` — are mapped to the
same page under `latest`.

The two sites link into each other (53 website → docs, 19 docs → website
`LINKS_TO` edges), sections repeated across them are deduplicated with the
docs copy kept, and the prompt context labels docs sources with their last
update date. Page nodes carry `site`; API sources carry `site` and
`last_modified` (API 1.1).

**Code blocks** keep their text exactly as written — line breaks,
indentation, `<br>` — and are fenced with ```` ``` ```` in section text, so a
command reaches the model as one line. Sphinx wraps every highlighted token in
its own `<span>`, and joining spans with newlines (as the importer did until
2026-10-26) turned `srun --partition=short ...` into one token per line.

The docs ingest found 5 broken links inside rc-docs (to pages from an older
layout such as `hardware/partitions.html` and `using-discovery/srun.html`),
listed in `ingest_report.json` under `docs.unresolved_links`.

## What the graph contains

Built 2026-10-26 (the published `full` graph): 170 pages → 597 sections
(2 pages have no prose sections; see *Known gaps*).

| site | content type | pages | sections | retrievable |
|---|---|---|---|---|
| rc.northeastern.edu | page | 33 | 221 | 168 |
| | post | 56 | 115 | 84 |
| | calendar_event | 24 | 47 | 0 by default |
| rc-docs.northeastern.edu | doc | 55 | 214 | 214 |

Graph: `Page=170  Section=597  Topic=30  Category=13  ExternalLink=169`,
`HAS_SECTION=597  NEXT=429  LINKS_TO=461  LINKS_OUT=298  MENTIONS=1819
IN_CATEGORY=80  CHILD_OF=15`. Every section has a 384-dim `bge-small-en-v1.5`
embedding. `LINKS_TO` includes 81 website → docs and 19 docs → website
edges.

41 sections are hidden from retrieval as duplicates: 23 texts repeat across
pages (the "How Can Research Computing Support You?" footer appears 26 times;
staff bios are repeated on every event page). One canonical copy stays
retrievable and the others carry `duplicate_of`.

43 more are kept in the graph but not retrieved because they are about people,
not support (`ingest/run.py`, `_mark_not_support`): the Research Computing Team
and From the AVP pages, posts in the Researcher Spotlight and From the AVP
categories, and 6 bios repeated elsewhere (the AVP's on the home page). On 19
real tickets these were in the top 6 whenever a ticket named a person or
carried a signature -- the AVP is CC'd on every ticket.

Feed sections keep only their lead (`feed_lead`): a section whose bullets are
the titles of posts already in the graph. The Status Updates page was one
11,682-character section listing every status post; it matched any ticket
about storage, maintenance or access, and only its first 2,500 characters ever
reached the model. Now it is its first paragraph ("The Explorer Cluster is
fully operational ..."), and each update is retrieved as its own dated post.
The same rule trims the Blogs, Tech Tool Tips and From the AVP indexes.

Retrieval regressions from reviewed tickets are in `eval/ticket_cases.yaml`
(`python -m eval.ticket_cases`): pages a ticket must not get, pages it must,
and tickets no page covers, which must come back `low_confidence`.

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
(:Page {site, content_type, title, url, published, last_modified, slug, wp_id})
        site: rc.northeastern.edu | rc-docs.northeastern.edu
        content_type: page | post | calendar_event | doc     (wp_id: WordPress only)
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
   Status posts (outages, maintenance, purges) older than 30 days have their
   score multiplied by 0.6 unless the query is itself about an outage or
   maintenance: last April's "Explorer and Discovery are Down" is not why one
   node is down today.
2. **Graph expansion,** per seed: neighbouring sections (`NEXT`, both ways),
   linked RC pages (`LINKS_TO`), linked external docs (`LINKS_OUT`, mostly
   rc-docs, where the commands live) and topics.
3. **Output:** numbered sources `[S1]…` with exact URLs and publish dates, a
   `context` block ready to paste into a prompt, `allowed_urls`, and
   `low_confidence` when the best vector match is under 0.83 (the eval
   questions score 0.84–0.93; off-topic questions such as "how do I reset my
   Canvas password" at most 0.80). Problems no page covers are also flagged
   by what the query says (`known_gap`): one node down or drained, or the
   cluster being slow -- on real tickets their scores (0.83-0.88) overlap the
   covered ones, so no threshold can catch them. Calibrated on short queries -- TicketMate
   sends the subject, description and the researcher's latest message, not the
   whole thread, which scores high whatever it says.

## API contract and versioning

[API_CONTRACT.md](API_CONTRACT.md) lists every request and response field
clients may rely on. `/health` returns `api_version` (`1.0`); clients check
the major number and refuse to guess across a mismatch — TicketMate drafts
ungrounded, with a caveat naming the versions. Bump MAJOR for anything that
could break an existing client, MINOR for additions; tag releases (`v1.0.0`).

`scripts/contract_test.sh` starts a fresh instance and asserts every field in
the contract.

**Tracing (API 1.3).** `/retrieve` and `/cite` return a `trace` (per-stage
timings, candidate counts, ranking settings, best vector score, graph
snapshot) and echo the caller's `X-Request-ID`. rc-kg logs one line per
request with that id, timings and counts — never the query, which may be
ticket text. TicketMate stores the full trace of each draft in the user's
private run log. TicketMate's `tests/rc_kg/run_contract.sh` checks
the other side: grounding, citation numbering and flags, a v2 rc_kg refused,
and rc_kg down. Run both before tagging or deploying a change to either.

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

The same call sets `check_support`, and every citation whose page does not
seem to say the sentence becomes a caveat for the engineer, naming the page
that fits better when there is one. Measured by `eval/citation_check.py` on
the two-site graph: hand-labelled draft sentences 0/12 correct citations
flagged, 6/6 wrong ones caught; 265 held-out reworded sentences 0% flagged,
83% caught. Two sites often both support a claim, so a citation is only
flagged when another page beats it clearly (margin 0.10) and it is not itself
a confident match (score below 0.95).
Those are proxies — real drafts are the test (next section).

### Checking real drafts

`eval/citation_review.py` reads TicketMate's run logs
(`/scratch/$USER/rc_copilot_runs`, which keep the exact prompt and the raw
model output), reports how often grounded drafts cite, how many citations are
flagged, `[S#]` markers naming no source and URLs the model wrote, and writes
every citation to a CSV (mode 600 — it quotes drafts) with an empty
`human_label` column:

```bash
# with a TicketMate session running a real model (its log shows node:port):
python3 eval/draft_batch.py --url http://<node>:<port>    # 30 eval tickets
scripts/with_kg.sh -m eval.citation_review --only RCKG-EVAL
```

Label a sample of rows right/wrong and the support check's real error rate on
your model is known rather than assumed.

Ticket text never leaves the cluster: embeddings run in-process, and the API
binds `127.0.0.1`.

## Evaluation framework

Three layers, each answering a different question. All run against a private
copy of a snapshot via `scripts/with_kg.sh`; add `RC_KG_VARIANT=website` and
`--sites rc.northeastern.edu` for the website-only graph.

### 1. Semantic retrieval (RAGAS, no LLM) — `eval/retrieval_eval.py`

```bash
scripts/with_kg.sh -m eval.retrieval_eval [--json report.json]
```

Relevance comes from the labels in `eval/questions.yaml` (a retrieved
section is relevant if its page is an expected page), so it runs anywhere and
gates builds. Every mean has a 95% bootstrap interval — at this eval size, a
difference inside it is not evidence.

| metric | what it asks | full graph (45 q) | website graph (31 q) |
|---|---|---|---|
| hit@5 | is an answering page in the top 5? | 0.978 [0.93, 1.00] | 1.000 |
| hit@1 | …at rank 1? | 0.689 [0.56, 0.82] | 0.742 [0.58, 0.87] |
| MRR | how high is the first answer? | 0.797 [0.70, 0.88] | 0.855 [0.76, 0.94] |
| nDCG@5 | rank quality over all answering pages | 0.787 | 0.791 |
| **RAGAS context precision** | are relevant contexts ranked above irrelevant ones? (average precision) | 0.760 [0.68, 0.84] | 0.784 |
| **RAGAS ID context precision** | share of retrieved pages that are labelled relevant | 0.360 | 0.365 |
| **RAGAS ID context recall** | share of labelled pages retrieved | 0.919 | 0.866 |

The RAGAS formulas are copied from ragas 0.4.3 and checked against the
library on the same inputs (`tests/test_metrics.py`); RAGAS normally gets the
relevance verdicts from an LLM, here they come from the labels. Two caveats
in reading them: **ID context precision is a lower bound** — the labels are
not exhaustive, so relevant pages nobody listed count as irrelevant; and
**ID context recall asks for every listed page**, while the labels list
alternatives, so hit@k is the measure of "found an answer".

On the 24 website-answerable questions, the full and website graphs do not
differ measurably (MRR 0.833 [0.73, 0.93] vs 0.861 [0.76, 0.95]): adding the
docs did not cost website answers.

### 2. Graph relations — `eval/graph_eval.py`

```bash
scripts/with_kg.sh -m eval.graph_eval [--live] [--json report.json]
```

| part | what it checks | result (full graph) |
|---|---|---|
| integrity | 16 invariants over every relation: one page per section, NEXT is the reading order, CHILD_OF acyclic, LINKS_TO records its href, duplicate_of points at a valid canonical, unique text and URLs among retrievable sections, embeddings present, every MENTIONS edge re-matched against its text (1,846 edges) | **all pass** — a hard gate in `build_graph.sh` |
| live validity (`--live`) | every cited section URL lands where it claims on the live site (`#id` exists, text-fragment heading present); every LINKS_TO edge's href resolves to its target | section links **99.6%** (496/498); LINKS_TO **100%** (461/461) |
| usefulness | do relations put answer-bearing pages in front of the model? | page recall@5 0.919 → 0.930 with linked pages (no question answered *only* via a link); topic signal on vs off: MRR 0.797 vs 0.789 — **no measurable effect** |
| topics | coverage, specificity, alias dominance | 93.8% of sections have a topic; "Explorer cluster", "Storage", "Slurm" each cover >25% (IDF-downweighted in ranking); "Discovery cluster" matched almost entirely by the ambiguous word "Discovery" |
| topology | components, unlinked pages, cross-site edges | 151 of 170 pages in one component; 20 retrievable pages nothing links to; 54 website→docs and 17 docs→website page edges |

What this says plainly: the graph's relations are **correct** (integrity and
live validity), and they earn their keep in **citations** (exact anchors,
dedupe, cross-site link mapping) and **context** (neighbouring sections,
linked pages shown to the model) — but they **barely move ranking** on this
eval. The two live failures are sections headed "Overview" whose
text-fragment target is not visible on the rendered page (a browser lands at
the page top instead).

### 3. LLM-judged RAGAS — `eval/ragas_llm.py`

```bash
scripts/eval_env.sh                                        # once: .venv-eval
scripts/ragas_eval.sh --judge-url http://<node>:<port>/v1 --questions
scripts/ragas_eval.sh --judge-url http://<node>:<port>/v1 --runs /scratch/$USER/rc_copilot_runs --only RCKG-EVAL
```

`--questions`: ContextRelevance on the eval questions (catches relevant pages
the labels miss). `--runs`: Faithfulness, ResponseGroundedness and
ContextUtilization on real TicketMate drafts (e.g. those `eval/draft_batch.py`
produces). The judge must be a vLLM inside the university — ticket text goes
to it. It runs in its own `.venv-eval` (`requirements-eval.txt`; ragas 0.4.3
needs the langchain 0.3 line) and reaches the graph through the HTTP API.
Plumbing is tested end to end with `tests/stub_judge.py`; **it has not yet
run with a real judge model**, so no LLM-judged scores exist yet.

## Adding topics and eval questions

**Topics** live in `topics.yaml` (`name`, `description`, `aliases`). A section
MENTIONS a topic only when an alias appears in it literally (whole word,
case-insensitive), so every edge is explainable and nothing is invented.
Re-run `python -m enrich.run` after editing; topics removed from the file are
removed from the graph.

**Eval questions** go in `eval/questions.yaml` as `question` + `expected_urls`
(page URLs). Phrase them like tickets, not like headings — and include terse
ones: "How do I start Jupyter on the cluster?" failed while a longer wording
passed, until keyword search learned to prefix-match (`Jupyter*` →
JupyterLab). **Citation cases** go in `eval/citation_cases.yaml`; only label a
page unsupported after reading it (a "staff bio" heading turned out to hold
seff and job-array tips).

---

## Phase status

| Phase | State |
|---|---|
| 1 — ingestion (WP REST API + rc-docs) | **done**, verified against both live sites; 170 pages, 597 sections |
| 2 — topics and embeddings | **done**: 30 topics, 1,819 MENTIONS, every section embedded |
| 3 — GraphRAG retrieval + HTTP API | **done**: `/retrieve`, `/cite` (+ citation-support check), `/similarity`, `/answer`; serving real TicketMate drafts |
| 4 — incremental sync and link health | `modified_after`, `content_hash` and `embedded_key` are in place; no runner (a full rebuild takes ~4 min, weekly via `rebuild.sbatch`) |
| 5 — evaluation | **done**: Hit@1 0.689, Hit@5 0.978, MRR 0.795 on 45 questions; 9/9 reviewed-ticket cases (`eval/ticket_cases.py`); variant comparison (`eval/variants.py`); citation check; `citation_review.py` for real drafts |

### Known gaps

- `/aicr/aicr-gpu-dashboard/` yields no sections: its body is a plugin
  shortcode with no prose. Correctly reported, not silently skipped.
- Many sections are short (on the 2026-10-22 website-only build, 101 of 360
  were under 200 characters), which is small for reliable embedding. Merging
  them into their parent is proposed, not done.
- 6 internal links are genuinely broken (5 are PDFs under `/files/2025/08/`).
- `/aicr/rc-internal` also yields no sections (same dashboard plugin).
- The eval sets (45 retrieval questions — 30 website, 15 docs — and 10
  citation cases) were written by one person from the page inventories. They
  catch regressions; they are too small to tune weights on, which is why only
  changes with a clear cause were made (see `_TOPIC_MODE` in
  `retrieval/graphrag.py`).
- "How do I add a student to my /projects directory?" misses the top 5 even
  though vector search ranks the right docs section first: keyword search
  cannot match "add" to "Adding" (3-letter words get no prefix form), and rank
  fusion rewards sections both searches find over one search's clear winner.
- Real Qwen2.5-7B drafts do not always cite the sources they were given
  (INC09000005 cited neither of two relevant srun pages); TicketMate now ends
  the prompt with a citation checklist and flags uncited grounded drafts.
  `citation_review.py` has not yet been run over a batch of real drafts.
- The citation check trades recall for trust: at its current strictness it
  misses about 1 in 6 wrong citations on held-out sentences, and it cannot
  flag a wrong citation when no retrieved page supports the claim (there is
  no better source to point to).
- The weekly rebuild (`scripts/rebuild.sbatch`) has to be scheduled by its
  owner with `scrontab` (see the script's header); until then the graphs
  only change when someone runs `build_graph.sh`.

---

## Layout

| Path | What it is |
|---|---|
| `ingest/` | WP API client, sitemap reader, sectioner, anchors, URL rules, orchestrator |
| `graph/` | Neo4j driver, schema (constraints + indexes), batched writer |
| `topics.yaml` | The closed topic vocabulary |
| `enrich/` | Phase 2 — topic matching and embedding |
| `retrieval/` | Embedding providers, GraphRAG retriever, citation enforcement and support check, CLI |
| `api/` | FastAPI service for TicketMate |
| `API_CONTRACT.md` | The v1 HTTP contract clients rely on |
| `eval/` | Retrieval: `run.py` (build gate), `ticket_cases.py` (regressions from reviewed tickets), `variants.py` (website-only vs full, same tests), `retrieval_eval.py` + `metrics.py` (RAGAS + ranking, CIs), `ragas_llm.py` (LLM-judged). Graph: `graph_eval.py`. Citations: `citation_check.py`, `citation_review.py`, `draft_batch.py`. Contract: `contract_check.py` |
| `scripts/` | `build_graph.sh`, `rebuild.sbatch` (weekly), `build_runtime.sh`, `serve_kg.sh`, `with_kg.sh`, `kg_ps.sh`, `contract_test.sh`, `eval_env.sh`, `ragas_eval.sh` (sharing `_kg_instance.sh`); dev Neo4j install / start / stop / dump |
| `tests/` | 92 unit tests: code blocks, sectioning, anchors, URLs, duplicates, people and feed pages, topics, citations, claims, query building |
