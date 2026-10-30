---
name: 7. Playbooks (skills)
route: /playbooks
menu: The pipeline
---

# 7. Playbooks (skills)

> **New:** `form.<field>` lines (a request-form question per field) and `rubric:` lines (what the LLM judge grades a reply to this request type on): chapter 19.

Some tickets are not answered from the website at all. RC has a set
procedure for them: membership of a project group, access to a restricted
partition, a storage increase, a course allocation. A **playbook** is one such
procedure written as a Markdown file: a header that says which tickets it is
for, then the runbook itself -- its stages, how to tell from the thread which
stage a ticket is at, the commands for each stage and the reply for it.

When a ticket matches, the runbook goes into the prompt and the draft follows
it: `suggested_steps` are the next stage's commands with the ticket's values
filled in, `draft_response` is that stage's reply. **Nothing here runs a
command.** Several procedures change things (a reservation, a group); the RC
engineer runs them.

Code: `ticketmate/backend/app/playbooks.py` (215 lines, no dependencies
beyond the standard library).

## 7.1 Where playbooks live

RC's procedures are internal, so they are **not in the repository**.

| directory | what | used when |
|---|---|---|
| `$PLAYBOOK_DIR` | RC's real playbooks, one `*.md` per procedure, kept privately on `/projects` | the variable is set |
| `ticketmate/backend/app/playbooks/` | one generic, made-up example (`example-procedure.md`) that shows the format | `PLAYBOOK_DIR` is unset |

`playbooks.playbook_dir()` returns `$PLAYBOOK_DIR`, else the bundled
directory. `cluster/env.sh` sets `PLAYBOOK_DIR` to the team's private
directory when that directory exists and the variable is not already set;
both launchers (`ood-apps/rc-copilot/template/script.sh.erb`,
`cluster/serve_stack.sbatch`) source `env.sh`, so every session on Explorer
uses RC's real playbooks. A checkout elsewhere uses the example.

The setting `PLAYBOOKS` (`Settings.playbooks`, default `true`) switches the
whole feature off: `main.draft` then passes no playbooks to the prompt.

> Do not commit a real procedure into the repository. Both repositories are
> public. Write and test procedures in `$PLAYBOOK_DIR`; use made-up content in
> anything under `ticketmate/`.

## 7.2 The file format

A file starts with a `---` line, has a header of `key: value` lines, a
closing `---` line, and then the runbook body (free Markdown). This guide uses
a made-up procedure as its example:

```markdown
---
# A fictional procedure: a temporary increase of a user's scratch quota.
name: scratch-quota
title: Temporary scratch quota increase
# Every `match` line must match the researcher's words; no `unless` line may.
match: (?i)\bscratch\b
match: (?i)\b(quota|more space|increase|extend)\b
unless: (?i)\b(purge|purged|deleted)\b
# rc-agent: which specialist agent this kind of ticket belongs to.
route: request
# Values pulled out of the whole cleaned ticket (group 1, or the whole match).
field.username: (?<![\w@./-])([a-z][a-z0-9-]*\.[a-z][a-z0-9-]*)(?![\w@-]|\.[a-z0-9])
field.email: \b([a-z0-9._-]+@example\.edu)\b
field.size: (?i)\b(\d+)\s*TB\b
field.ticket: (?m)^Ticket:\s*(\S+)
# What RC needs before it can act; any one listed field satisfies it.
require.username|email: the cluster username (or email) of the account
require.size: how much space is needed, and for how long
---
Scratch quota increases are temporary and approved by RC.

Stages -- read the RC staff entries to see which one the ticket is at:

1. NEW REQUEST. Steps for the RC engineer: check the account
   (`id <username>`), check current use. Reply: RC is reviewing the request.

2. APPROVED (an RC staff entry says so). Steps: apply the increase.
   Reply: say it was applied only once an RC staff entry or a lookup shows it.
```

### Header keys

| key | repeat | value | rule |
|---|---|---|---|
| `name` | once | the playbook's id, e.g. `scratch-quota` | required; shown in traces, tags and metrics |
| `title` | once | the heading the prompt shows | required |
| `match` | yes | a Python regular expression | at least one; **every** `match` must find something |
| `unless` | yes | a Python regular expression | **no** `unless` may find anything |
| `on` | once | `researcher` (default) or `instructions` | which text `match`/`unless` read (7.3) |
| `route` | once | `request` (default) or `incident` | read and validated in both apps; used only by rc-agent (7.8) |
| `field.<key>` | yes | a regular expression | a value to pull out of the ticket (7.4) |
| `require.<key>[\|<key>...]` | yes | what to ask the researcher for | a requirement satisfied by any one listed field (7.5) |

Parsing rules (`playbooks.parse`):

- The file must start with exactly `---\n`, and the header must end with a
  `\n---\n` line. Otherwise `ValueError` ("no header", "header is not closed").
- Blank lines and lines starting with `#` in the header are skipped.
- A line without a colon, or with an empty value, is an error with its line
  number. So is an unknown key.
- Patterns are compiled with `re.compile`; flags go inline (`(?i)`, `(?m)`).
  A bad pattern is an error naming the line.
- `route` must be `incident` or `request`; `on` must be `researcher` or
  `instructions`.
- `name`, `title` and at least one `match` are required.
- Every key named by a `require.` line must have a `field.` line (in any
  order in the header); otherwise `require.<keys> names no field.<key> line`.

### Loading

`playbooks.load(directory="")` reads every `*.md` in the directory **sorted by
file name**, parses each, and returns the list. A file that does not parse is
**skipped with an error in the log** (`playbook skipped: ...`), so one bad edit
cannot stop drafting.

`load` is wrapped in `functools.lru_cache`: the directory is read once per
backend process. An edited playbook is picked up by the next session (or a
backend restart), not by a running one. Tests call `playbooks.load.__wrapped__()`
to bypass the cache.

## 7.3 Matching: which text a trigger reads

`playbooks.find(researcher_text, ticket_text, playbooks=None, instructions="")`
is called once per draft, in `main.draft`, after retrieval and lookups and
before the ticket is fitted to the context window:

```python
found_playbooks = playbooks.find(prepared.query or ticket, ticket,
                                 instructions=request.extra_instructions)
```

| playbooks with | `match`/`unless` read | why |
|---|---|---|
| `on: researcher` | `prepared.query`: the retrieval query from `ticket_context.build_query` -- the subject, the researcher's words in the description, a catalog item's **request form**, and the researcher's latest message (at most 1,500 characters, `QUERY_MAX_CHARS`) | RC's own replies and notes cannot trigger a playbook. A thread where RC wrote "long partition" in a reply does not make the next draft a long-partition request |
| `on: instructions` | what the RC engineer typed for this draft (`extra_instructions`) | follow-up and closing skeletons are chosen by what the engineer asked for, never by the ticket |

`Playbook.applies(text)` is: at least one `match` line, all of them found
(`re.search`), and no `unless` found.

**Request forms.** A ServiceNow catalog item (an access request, a course
request) often has an empty description; its substance is the requester's
answers to the form. The extension sends them under a `Request form:` label
(`extension/content.js`, one `- question: answer` per line), and
`build_query` includes that block in the query. So a playbook's `match` lines
see a form answer such as `- Partition requested: ...` exactly as they see
the description. Write `match` patterns that also fire on the form's wording.

**Order and limits.**

1. Request playbooks (`on: researcher`) that apply, in file-name order, at
   most `MAX_PLAYBOOKS = 2` (each runbook is about 1k tokens).
2. Then, only if the engineer typed instructions, at most **one**
   `on: instructions` playbook that applies.

So a draft has at most three playbooks, and request playbooks always come
first: they say what the ticket is about.

**Writing triggers.** The real playbooks' test (7.7) shows the pattern that
works: one `match` for the subject of the request, one for the act (access,
add, request, join), and `unless` lines for look-alikes. A ticket about a
*drained* node on a restricted partition is an incident, not an access
request; an `unless` on `drain(ed)?` keeps the access playbook out.

## 7.4 Values found in the ticket

`Playbook.extract(ticket_text)` runs every `field.<key>` pattern over the
**whole cleaned ticket** (`prepared.text`, every entry, staff notes
included) -- unlike the triggers. A project number that only an RC work note
gives is still found.

- The value is group 1 of the match, or the whole match if the pattern has no
  group; stripped.
- Values are kept in order of appearance, without repeats, at most
  `MAX_FIELD_VALUES = 6` per field.
- A field with no value is left out of the result.

The result is a `Match(playbook, values)`. The prompt tells the model the
values are pattern matches to check, not facts.

## 7.5 Missing information

`require.<key>[|<key>...]: <ask>` says what RC needs before it can act. It is
**satisfied** when any one of the listed fields has a value. `Match.missing`
lists the `<ask>` text of every unsatisfied requirement, in header order.

For the example above, a ticket that says only "please increase my scratch
quota" from `ada.lovelace@example.edu` has an email, so `username|email` is
satisfied; no size was found, so `missing` is
`["how much space is needed, and for how long"]`.

What happens with it:

| where | what |
|---|---|
| prompt, workflow block | a `MISSING -- not found in the ticket, and needed before RC can act:` list under the playbook's title |
| prompt, workflow rules | first check the ticket for it in another form (a name instead of a username, a project's title instead of its number); if only the researcher can supply it, the reply asks for exactly those items as a short list and does not say the request is being processed; if RC can find it itself, finding it is a step; never ask the researcher for their *own* username or email |
| prompt, checklist | `- The reply asks the researcher for what the RC WORKFLOW still needs, unless the ticket gives it another way: <items>.` |
| trace | tag `playbook:missing_info` on the root run |

`playbooks.missing_text(matches)` joins the missing items of every matched
playbook, in order, without repeats; `main.draft` passes it to the prompt as
`playbook_missing`.

## 7.6 How the prompt uses a playbook

`playbooks.prompt_text(matches)` renders each match as:

```text
### Temporary scratch quota increase

Values found in the ticket (check them; they are pattern matches):
- email: ada.lovelace@example.edu
- ticket: INC0000042

MISSING -- not found in the ticket, and needed before RC can act:
- how much space is needed, and for how long

Scratch quota increases are temporary and approved by RC.
...the rest of the runbook body...
```

(`- (none)` when no value was found.) Blocks are joined by blank lines and
placed in `prompts.PLAYBOOK_TEMPLATE`, the `--- RC WORKFLOW ---` block, which
comes after the ticket, lookups and sources and **last before the engineer's
instructions**: a 7B model acts on what it read last, and the procedure
decides what the reply is. The block's fixed rules tell the model to:

- work out the stage from the RC staff entries (the newest decide; a stage is
  done only if a staff entry or a lookup shows it);
- write that stage's steps, in order, with the ticket's values filled in --
  never an invented value;
- write that stage's reply in its own words; commands only RC runs go in
  `suggested_steps`, never in the reply, unless the procedure's reply gives
  the researcher a command;
- handle `MISSING` as in 7.5.

The checklist at the end of the prompt adds
`- The steps and the reply are those of the RC WORKFLOW stage this ticket is at.`
whenever a playbook matched. Chapter 8 shows the full prompt order.

**Links in a runbook.** `Match.urls` collects every `http(s)://` URL in the
runbook body (trailing `.,;:` stripped). `main.draft` passes them to
`kg_client.apply_citations` as `extra_allowed_urls`, so a link the procedure
itself gives survives the citation check (chapter 10), which otherwise removes
any URL retrieval did not return.

**Token budget.** The rendered playbook text counts against the context
window: `main.draft` reserves `len(playbook_context) / 3` tokens for it
before fitting the ticket (chapter 8.6).

### Follow-up and closing skeletons

A playbook with `on: instructions` is a *reply* playbook: RC's own short
wording for a follow-up ("checking in on our last message") or a closure
("closing this ticket as we have not heard back"). It matches the engineer's
instruction, typos included, and its body gives one skeleton per case. The
system prompt already says the engineer's instructions decide what the reply
is; the skeleton gives it RC's length and wording.

Such instructions usually also make `prompts.instruction_wants_sources`
return `False`, so the website sources are left out of the prompt and their
tokens go to the thread (chapter 8.4).

## 7.7 Tracing

| where | what |
|---|---|
| run tree | a `playbooks.match` run (type `tool`), inputs `query_chars`, outputs every match's `to_dict()`: `name`, `title`, `values`, `missing` |
| root run tags | `playbook:<name>` per match; `playbook:missing_info` when something is missing |
| run log `trace` | `playbooks` (the same dicts); `ticket_context.playbooks` (names) |
| metrics row (`traces/$USER/metrics/`) | `playbooks`: names |
| `ops/ops_report.py` | "skills used": the most frequent names |

## 7.8 Tests

`ticketmate/tests/playbooks/test_playbooks.py` (plain script, exit 0 = pass):

```bash
cd ticketmate && backend/.venv/bin/python tests/playbooks/test_playbooks.py
```

It tests **RC's real playbooks**: it sets `PLAYBOOK_DIR` to the private
directory when that exists, and **skips with exit 0** when no playbooks are
available. With them it checks:

| section | what |
|---|---|
| loading | every file parses, the expected set of names is present, every body is longer than 300 characters; malformed headers are rejected (no header, not closed, no `match`, bad pattern, unknown key) |
| routing | tickets worded like real ones (synthetic, no real people) pick the right playbook; look-alikes (a drained node, an OOM kill, a slow cluster) pick none |
| values | ids, emails in order, ticket numbers are extracted |
| missing information | every `require` names a field; a `require` naming no field is rejected; requirements satisfied by alternative fields; the checklist lists what is missing only when something is |
| triggers vs values | a value in an RC work note is found; words only in RC's replies trigger nothing |
| prompt | the workflow block and values reach the prompt, after the ticket; no playbook means no block and no checklist line; at most `MAX_PLAYBOOKS` request playbooks |
| skeletons | chosen only by the engineer's instruction, typos tolerated, request playbook first; runbook links are allowed through the citation check |
| sources | `instruction_wants_sources` for follow-up and closure instructions |

When you add or change a playbook, add a routing case for it and one
look-alike that must not match. Keep test tickets synthetic.

## 7.9 rc-agent: playbooks as skills

> **rc-agent:** the same files are **skills** (`app/agents/skills.py`). A
> skill is a playbook seen by the agents. `on: researcher` makes it a
> `procedure`; `on: instructions` makes it a `reply`. `skills.catalog()`
> gives each one's card: name, title, kind, `route`, its triggers (which text
> they read, the `match` and `unless` patterns) and its requirements.
>
> Skills reach a draft two ways, and the trace says which:
>
> | way | how |
> |---|---|
> | `trigger` | `skills.trigger()` = `playbooks.find()`, before any agent runs, exactly as in TicketMate |
> | `load` | a specialist agent calls the `load_skill` tool; its argument is a `Literal` of the procedure names, so validation refuses any other name before the handler runs; `skills.load(name, ticket)` extracts the values and adds the match to the shared evidence |
>
> Either way the writer gets the full runbook, the values and what is missing.
>
> **The `route` header** decides which agent a triggered procedure belongs
> to. The orchestrator (`agents/graph.py`, `_route`) applies rules before it
> asks a model:
>
> 1. the engineer gave instructions and a `reply` skill matched (or the
>    instruction needs no sources) -> `followup` (straight to the writer);
> 2. a `procedure` skill matched -> that skill's `route` (`incident` or
>    `request`);
> 3. the ticket number is a TASK/RITM/REQ -> `request`;
> 4. otherwise the model chooses, with `incident` as the default.
>
> Each skill card is hashed (first 8 hex digits of SHA-256) into the draft's
> `manifest`, so a trace records which version of every skill was live.
> Chapter 15 covers the agents.
