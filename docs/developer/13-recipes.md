---
name: 13. Recipes
route: /recipes
menu: Operations
---

# 13. Recipes

Step-by-step instructions for the changes you will make most often. Each
recipe names the files to touch, the commands to run, and how you know it
worked. Paths are relative to the repository root
(`/projects/rc/projects/ticketmate` or `/projects/rc/projects/rc-agent`).

A reminder before you start: a backend or rc-kg change is live in the next
OOD session without publishing, because sessions run the code straight from
the checkout. Only the OOD templates and the extension need
`ood-apps/publish.sh` (chapter 2).

## 13.1 Add a playbook

A playbook is one of RC's procedures as a Markdown file (chapter 7). RC's real
playbooks live in the private playbook directory (`PLAYBOOK_DIR`), not in the
repository. The repository holds only the format example,
`ticketmate/backend/app/playbooks/example-procedure.md`.

1. **Copy the example** into the private playbook directory under a new name,
   e.g. `software-access.md`. The file name sets the order playbooks are
   tried in (`playbooks.load` sorts by name).
2. **Write the header**, between two `---` lines. Lines starting with `#` are
   comments.

   | key | required | meaning |
   |---|---|---|
   | `name` | yes | identifier, shown in the trace and the panel |
   | `title` | yes | heading in the prompt |
   | `match` | at least one | a regex; **every** `match` line must match |
   | `unless` | no | a regex; **no** `unless` line may match |
   | `on` | no | `researcher` (default): `match` reads the researcher's words (subject, description, latest message). `instructions`: it reads what the RC engineer typed (follow-up and closing skeletons) |
   | `field.<name>` | no | a regex run on the whole cleaned ticket; group 1 (or the whole match) is the value; at most 6 values per field |
   | `require.<f>[\|<f>...]` | no | what RC needs before it can act; satisfied by any one of the named fields; the text after the colon is what the reply asks for. Every field named must have a `field.` line |
   | `route` | no | rc-agent only: `incident` or `request` (default), the agent a triggered skill sends the ticket to |

   Any other key, a bad regex, or a `require` naming no `field` makes the file
   fail to parse. A file that fails is skipped with an error in the log
   (`playbook skipped: ...`); it never stops drafting.
3. **Write the body**: the stages, how to tell from the RC staff entries which
   stage the ticket is at, the commands for the RC engineer at each stage, and
   the reply for each stage. Nothing in it is run. URLs written in the body
   are allowed through the citation check.
4. **Keep `match` narrow.** Matching is on the researcher's own words only, so
   RC's replies cannot trigger a playbook; but at most 2 researcher playbooks
   (plus 1 instructions playbook) go into one prompt (`MAX_PLAYBOOKS`), each
   about 1k tokens. Use `unless` for look-alikes (a drained node is not an
   access request).
5. **Test it.** In `ticketmate/tests/playbooks/test_playbooks.py`: add the
   new `name` to the `expected` set; add routing `CASES` worded the way real
   tickets word them, and look-alikes that must match nothing; add a values
   check if it has fields. Use synthetic text only.

   ```bash
   cd ticketmate
   PLAYBOOK_DIR=<private playbook directory> backend/.venv/bin/python tests/playbooks/test_playbooks.py
   ```
6. **See it in a draft.** `playbooks.load` is cached per process: restart the
   backend (or start a new session). The matched playbook appears in the
   panel, in `context` of the response, and in the run log.

> **rc-agent:** a playbook is a *skill*. Give it a `route:`. Its name is
> added to the `load_skill` tool's allowed values automatically
> (`agents/tools.py` builds them from `skills.procedures()`). Add a
> ground-truth case with `skills: [<name>]` to `ticketmate/evals/groundtruth.yaml`
> and run `tests/run_all.sh --quick` (stages 3 and 8).

## 13.2 Add a cluster lookup

Lookups are read-only commands run for what a ticket names (chapter 6). All of
it is in `ticketmate/backend/app/lookups.py`. Adding one changes what runs in
every engineer's job, so follow every step.

1. **Choose a read-only program** with a fixed argument list. If it can change
   anything, stop: it is a step for the engineer, written in a playbook.
2. **Add a strict pattern** for its argument next to `USERNAME`, `JOB_ID`, ...
   It is checked with `fullmatch`, so it describes the whole value.
3. **Add a candidate regex** (like `_NODE_CANDIDATE`) that finds the value in
   ticket text, with lookarounds so it does not fire inside paths, emails or
   longer words; a `MAX_<KIND>` cap; a field on `Candidates`; and the loop in
   `find_candidates` that calls `_first(...)` with the cap and the strict
   pattern.
4. **Write the lookup** as `async def _thing(name, timeout) -> Lookup`:
   - first line (rc-agent): `bad = _invalid("thing", name, PATTERN)` and return
     it if set;
   - build `argv` as a list; never a string, never a shell;
   - `rc, out, ms = await _run(argv, timeout)`;
   - return `Lookup(kind, target, " ".join(argv), ok, summary, out, ms)`, with a
     one-line `summary` for the panel.
5. **Wire it into `run()`**, guarded by `shutil.which("<program>")` so a node
   without it simply skips the lookup. If it only matters for some tickets,
   gate it like `_WAITING`/`_FAILING`.
6. **Update the allow-list** in the module docstring, the `cluster_lookups`
   comment in `config.py`, and `ClusterLookup.kind` in `models.py`.
7. **Test it** in `ticketmate/tests/lookups/test_lookups.py`: add the program
   to the fakes loop and give the fake a canned output; check that a ticket
   naming a valid value runs exactly the argv you expect (read `calls.jsonl`),
   that an invalid or hostile value runs nothing, and that the `pwned` file
   never appears.

   ```bash
   cd ticketmate && backend/.venv/bin/python tests/lookups/test_lookups.py
   tests/rc_kg/run_contract.sh
   ```

> **rc-agent:** to let agents call it, add a Pydantic argument model in
> `agents/tools.py` (`Field(pattern=_full(lookups.PATTERN), description=...)`)
> and a `Tool(...)` in `registry()`, with a description the model reads and
> the agents allowed to use it (`(INCIDENT,)` or both). Then
> `tests/agent/test_agent.py` and `tests/agent/test_prompts.py`.

## 13.3 Add a topic or a source to the graph

### A topic

Topics are a closed, hand-written vocabulary in `rc-kg/topics.yaml`
(chapter 9). A section `MENTIONS` a topic only when one of its aliases appears
in it literally (whole word, case-insensitive).

1. Add an entry:

   ```yaml
     - name: Job efficiency
       description: Checking how well a finished job used what it asked for.
       aliases: [seff, job efficiency, CPU efficiency, memory efficiency]
   ```

   Keep aliases specific: "job" would tag half the site.
2. Check matching: `cd rc-kg && .venv/bin/python -m pytest -q tests/test_topics.py`.
3. Rebuild the graph (13.5): `rc-kg/scripts/build_graph.sh`. The build runs
   `enrich.run`, which links topics; topics removed from the file are removed
   from the graph.
4. Read the topics part of `graph_eval.json` in the snapshot directory
   (coverage, specificity, alias dominance), or run
   `scripts/with_kg.sh -m eval.graph_eval`.

### A page or a question

New pages on the RC website or rc-docs need no code: the next rebuild crawls
them. To make sure retrieval finds one, add a ticket-like question with its
page URL to `rc-kg/eval/questions.yaml` (`question`, `expected_urls`); the
build gate scores every question.

### A new site

A new site is code, not configuration. Follow how rc-docs was added:
`ingest/rtd.py` discovers and cleans its pages and is called from
`ingest/run.py` (`docs_enabled`); its base URL and repository are settings in
`rc-kg/config.py`; `scripts/_kg_instance.sh` maps each graph variant to its
sites (`kg_variant_env`, `kg_variant_sites`). Add unit tests like
`tests/test_rtd.py`, and questions whose answers live on the new site.

## 13.4 Debug a bad draft from its run log

1. **Get the request id**, 8 hex characters. The extension panel shows it as
   `draft id <id>`, the web page as `draft <id>`. The ticket number names the folder.
2. **Find the run log.** Drafts are recorded under `<repo>/traces/$USER/runs`
   (or `/scratch/$USER/rc_copilot_traces/runs` if you cannot write the
   repository). `cluster/traces.py` finds the right one by default; `--dir`
   points it elsewhere (older logs: `/scratch/$USER/rc_copilot_runs`):

   ```bash
   cd ticketmate
   python3 cluster/traces.py list --ticket INC0000042
   python3 cluster/traces.py show ab12cd34
   python3 cluster/traces.py show ab12cd34 --prompt   # every model call's exact prompt
   python3 cluster/traces.py show ab12cd34 --io       # every run's inputs and outputs
   ```

   (`--dir`, `--since` and `--ticket` go before the subcommand.)
3. **Walk the draft in order** and find the first step that went wrong:

   | question | where to look |
   |---|---|
   | Did the model see the ticket correctly? | `input.ticket_text` is what the page gave; the cleaned, labelled thread is in the user message of `prompt`; the response's `context` counts what cleaning removed and kept |
   | Were the right facts looked up? | the lookups block of the prompt; the `cluster.lookups` run in `trace.run_tree` (every command and its output); the response's `lookups` |
   | Did retrieval find the right page? | `trace.retrieval.sources`; `low_confidence`; the `rc_kg.retrieve` run with its documents |
   | Did a playbook match, and with which values and what missing? | the workflow block of the prompt (values found, `MISSING`); `context.playbooks` in the response |
   | Did the model follow the prompt? | `raw_response` vs `draft`; `trace.llm.attempts` (`strategy`, `outcome`) |
   | Were links removed or citations flagged? | `trace.citations` (`removed_urls`, flagged refs); the draft's caveats |
   | Which rule failed? | `evaluations.assertions_failed`; the judge's grades in `<run>.feedback.jsonl` |

4. **Reproduce the step that failed**, without a GPU where you can:
   - retrieval: `cd rc-kg && scripts/with_kg.sh -m retrieval.query "the researcher's words"`
     (add `--context` to see the block the prompt gets);
   - the whole pipeline in-process, with a stub model: chapter 2.3;
   - the model: re-run on a GPU job with `tests/run_inference_test.sbatch`
     and `TICKET=<a file with synthetic text>`.
5. **Fix, then pin it**: add a regression case (13.7) so it cannot come back.
6. **Record the verdict** on the draft: `traces.py ... feedback ab12cd34 --key
   thumbs --score -1 --comment "wrong partition page"`, or the panel's
   feedback form.

If the session itself misbehaved, its logs are in OOD's output directory for
the session: `output.log`, `vllm.log`, `rc_kg.log`, and `rc_kg_progress/`
(with `refresh.log`, and `neo4j-logs/` if Neo4j failed to start).

> **rc-agent:** `trace.agent` adds the route and why, the plan, every tool
> call (arguments, result, whether it ran), the critic's and reviewer's
> verdicts, revisions, the path through the graph, why the agent stopped,
> and the versions of every prompt, skill and tool. Guardrail findings are
> also in `traces/$USER/guardrails/`. To see whether a fix helps across many
> past tickets, use `tests/agent/replay_eval.py` (chapter 17).

## 13.5 Rebuild or refresh the graph

### Automatically, while serving

Every session's `serve_kg.sh` starts `scripts/refresh_kg.sh` in the
background (unless `RC_KG_REFRESH_LOOP=off`). It first checks 2 minutes after
start, then every 20 minutes:

1. If a snapshot newer than the one being served has been published, it
   loads it into a second private Neo4j and calls `POST /admin/swap`; the
   old Neo4j is stopped.
2. Otherwise, if the published snapshot is 24 hours old or more and no other
   build holds the lock (`<data root>/.build-<variant>.lock`), it rebuilds
   with `build_graph.sh`, then switches as in 1.

Its log is `refresh.log` (and `refresh.log.build`) in the session's progress
directory. `GET /health` on rc-kg shows the served `snapshot` and the last
`swaps`. Settings: chapter 14.

### By hand

```bash
cd rc-kg
scripts/build_graph.sh                     # the full graph (website + rc-docs)
scripts/build_graph.sh --variant website   # the website-only graph
scripts/build_graph.sh --all               # both
scripts/build_graph.sh --min-hit 0.8       # loosen the gate
scripts/build_graph.sh --force             # publish even if the hit@5 gate fails
```

Runs anywhere with network access to the sites; about 4 minutes and ~100
requests per variant. It uses its own private Neo4j and:

1. ingests (`ingest.run`), links topics and embeds (`enrich.run`);
2. runs the eval gate (`eval.run --min-hit`, default 0.9), `retrieval_eval`
   and `graph_eval`; **graph integrity is a hard gate**: `--force` does not
   override it;
3. writes `manifest.json` (time, commit, variant, sites, counts, eval);
4. dumps, stages beside the target and renames each file into place, keeping
   the replaced snapshot as hard links in `previous/`;
5. refreshes the runtime archive (`build_runtime.sh`).

Snapshots are published to `$RC_KG_DATA_ROOT`, by default the shared
`/projects/rc/projects/rc-kg-data` (`graph/`, `graph-website/`), which both
apps read; a checkout without it uses its own `rc-kg/data/`. Running sessions
switch to the new snapshot within one check interval.

### As a Slurm job

```bash
mkdir -p /scratch/$USER/rc_kg_rebuild          # once: the job's log directory
sbatch rc-kg/scripts/rebuild.sbatch            # both variants, CPU only
```

For a weekly rebuild, `scrontab -e` and add the two lines given in the header
of `rebuild.sbatch`.

### Rolling back

There is no rollback script. `previous/` in the variant directory holds the
snapshot the last publish replaced. Note that running sessions switch only to
a snapshot whose `built_at` is *later* than the one they serve, so a restored
older snapshot reaches new sessions only.

## 13.6 Ship a new extension version

1. Edit the files in `ticketmate/extension/`. While developing, load the folder
   itself with *Load unpacked* on `chrome://extensions` and click reload
   after each change.
2. Test (12.5):

   ```bash
   cd ticketmate
   backend/.venv/bin/python extension/check.py
   tests/ui/run.sh
   ```

   plus `extension_form_test.py` if you touched page reading, and
   `extension_startup_test.py` if you touched the startup display.
3. Bump `"version"` in `extension/manifest.json`. Chrome shows it, and it is
   how a member can tell they loaded the new build.
4. Package: `extension/package.sh`. It runs `check.py` first and refuses on
   failure, then writes `dist/rc-copilot-extension.zip` (built beside the
   target and renamed, so a failed run leaves no half zip).
5. Commit, including `dist/rc-copilot-extension.zip` (it is the one zip git
   keeps).
6. Publish: `ticketmate/ood-apps/publish.sh`. It copies the OOD app and the
   zip into `releases/<date>-<time>-<commit>/` under the release directory
   (`$RC_COPILOT_RELEASES`, default `<repo>/rc-copilot/`), writes `VERSION`,
   and atomically points `current` at it. Uncommitted changes in
   `ood-apps/rc-copilot` or `dist/` are published with a `-dirty` suffix. The
   last 3 releases are kept.
7. Check: `ood-apps/publish.sh --list` marks the live release with `*`;
   `ood-apps/publish.sh --rollback` steps back one.
8. Tell members to download the zip again and reload it: the extension does
   not update itself.

Before publishing template changes, run `ood-apps/rc-copilot/check_modes.sh`:
OOD copies file modes verbatim, and a template at 644 kills the session with
"Permission denied".

## 13.7 Add a reviewed ticket as a regression case

When an engineer finds a draft that went wrong, turn it into a case so the
failure cannot return.

1. **Reword the ticket.** Keep the words that drive retrieval: the subject,
   the description, the researcher's latest message (what TicketMate sends
   rc-kg). Replace every name, username, email, NUID, job id and lab or
   project name with a synthetic one ("Ada Lovelace", `lovelace.a`, job
   `4812345`). Never paste from the run log as is. Use an id that is not a
   real ticket number.
2. **Add the case** to `rc-kg/eval/ticket_cases.yaml`:

   ```yaml
   cases:
     - id: case-gpu-node-quota
       why: "quota question about a GPU node retrieved the status page"
       query: |
         GPU node out of space

         My jobs on the gpu partition fail with "Disk quota exceeded". How much space do I have?
       must_not:
         - https://rc.northeastern.edu/status-updates
       expected:
         - https://rc.northeastern.edu/<the page that answers it>
   ```

   | key | meaning |
   |---|---|
   | `id`, `why` | a name, and what went wrong |
   | `query` | the reworded researcher's words |
   | `must_not` | pages this case must not retrieve |
   | `expected` | at least one must be in the top k (6) |
   | `low_confidence: true` | no page covers the problem, so the flag must be up |
   | top-level `never` | pages no case may retrieve |

   Matching is at page level: no fragment, no trailing slash. An expected page
   on a site the graph lacks is skipped, not failed (`--sites`).
3. **Run it**, before your fix (it should fail if it reproduces the problem)
   and after:

   ```bash
   cd rc-kg
   scripts/with_kg.sh -m eval.ticket_cases          # failures only; exit 1 if any
   scripts/with_kg.sh -m eval.ticket_cases --all    # every case with its top k
   ```

> **rc-agent:** also add the case to `ticketmate/evals/groundtruth.yaml`, which
> labels the whole handling, not just retrieval: `subject`, `description`,
> `thread`, and any of `route`, `skills`, `must_lookup`, `expected_sources`,
> `must_not_sources`, `low_confidence`, `must_ask`, `reply_must`,
> `reply_must_not`, `reference_reply`. Mark it `source: synthetic` if it is.
> Run `backend/.venv/bin/python evals/run.py --suite rag --suite routing`
> (and `--suite e2e` on a GPU job). `tests/run_all.sh` stages 11 and 12 run
> both files.

## 13.8 Update the model

The model a session serves is chosen by name (`MODEL`) and mapped in one
script. Adding or changing a model touches four files.

1. **Download the weights on a login node.** Compute nodes run with
   `HF_HUB_OFFLINE=1` (`cluster/env.sh`) and never fetch from the Hub:

   ```bash
   source ticketmate/cluster/env.sh
   HF_HUB_OFFLINE=0 python -c "from huggingface_hub import snapshot_download; \
       snapshot_download('Qwen/Qwen2.5-7B-Instruct-AWQ')"
   ```

   Weights go to `$HF_HOME` (`<repo>/.cache/huggingface`).
2. **Map the name** in `ticketmate/cluster/serve_glm.sh`: a `case` entry
   setting `MODEL_ID`, `SERVED_NAME`, `DEFAULT_TP`, `MAX_LEN` and
   `GLM_PARSERS` (1 only for GLM models; GLM parsers make other models fail at
   startup), and add the name to the error message listing valid names.
3. **Tell the OOD session** in `ood-apps/rc-copilot/template/script.sh.erb`:
   the `TP`/`MAX_LEN` override for the model, and its `SERVED_NAME_HINT` (the
   backend starts before vLLM, so it needs the served name in advance; if the
   hint is wrong the script restarts the backend with the real one).
4. **Offer it in the form**: an option under `rc_model` in
   `ood-apps/rc-copilot/form.yml`; in `submit.yml.erb`, pin it to the GPU
   layout it needs (as `qwen7b` -> `1x40`, `flash` -> `1x80`).
5. **Test inference** on a GPU job (12.7):

   ```bash
   cd ticketmate/tests
   sbatch --gres=gpu:a100:1 --export=ALL,MODEL=<name> run_inference_test.sbatch
   ```

   Then a full stack with `cluster/serve_stack.sbatch` and `MODEL=<name>`.
6. **Publish** the OOD app (`ood-apps/publish.sh`, after `check_modes.sh`).

To upgrade vLLM itself, `cluster/install_vllm.sh --check` reports what is
installed; `VLLM_VERSION` and `CUDA_TAG` (default `0.29.0`, `cu129`) choose
the build. Read `VLLM_SETUP.md` first: the default PyPI wheel does not run on
Explorer's driver.

> **rc-agent:** the agents need tool calls. `serve_glm.sh` adds
> `--enable-auto-tool-choice --tool-call-parser hermes` for Qwen models
> (`QWEN_TOOLS=0` turns it off); GLM models get their own parser. A model
> served without tool calls still drafts: the agent stops with `tools
> unsupported` and the pipeline's draft is used (contract scenario G). Run
> `evals/run.py --suite e2e` against the new model before adopting it.
