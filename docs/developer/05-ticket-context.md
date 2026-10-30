---
name: 5. Ticket context
route: /ticket-context
menu: The pipeline
---

# 5. Ticket context

`ticketmate/backend/app/ticket_context.py` turns the scraped page into what
the model, retrieval and the lookups read. It has three public functions:

| function | input | output |
|---|---|---|
| `prepare(ticket_text)` | the text the extension sent | `Prepared(text, raw_chars, stats, query)`: the cleaned ticket, labelled and ordered, and the retrieval query |
| `build_query(head, latest)` | the ticket's head and the researcher's latest message | the retrieval query (also called by `prepare`) |
| `fit(prepared, max_chars)` | a `Prepared` and a character budget | a `Prepared` that fits, with what was left out in its stats |

Nothing in this module calls the model. It is pure text processing, and
`tests/ticket_context/test_ticket_context.py` tests it without a backend.

## 5.1 Why it exists

The module docstring records the measurement. On 18 real tickets
(2026-09/10) a typical page carried ~35 email addresses, nearly all of them
the RC team on every *Email sent* notification, and the notifications
outnumbered the real comments two to one. With the 4,000-character cut the
extension then applied, the noise crowded the older messages out. And
searching the whole thread matched RC's own words: the auto-acknowledgement
pulled training and office-hours pages into 15 of 19 tickets, and staff names
and signatures matched staff bios.

So, before anything else reads the ticket:

- **dropped:** *Email sent* notifications, every To/Cc/From line and address
  list, *Show email details*, avatar initials, signatures after a dashed
  rule, quoted reply chains, ServiceNow's automatic acknowledgement;
- **compressed:** *Field changes* to one line, *Email received* to its subject;
- **kept verbatim:** every comment and work note, with date, author and who
  could see it, **oldest first**, so the last entry is the latest;
- **labelled:** each author as *RC staff* or *researcher*.

## 5.2 What comes in

The extension sends (chapter 3):

```
Ticket: INC0000042

Short description: job killed

Description:
...

Request form:                       (catalog items only)
- <question>: <answer>

Work notes / activity:
<the activity stream as the page renders it, newest first>
```

`ACTIVITY_LABEL = "Work notes / activity:"` separates the **head** (number,
short description, description, request form) from the **activity**.

The request form is taken out of the head before the head is cleaned
(`_split_form`) and put back, unchanged, after it (`_with_form`). Cleaning cuts
a description at a signature rule (`-----`) or a quoted chain (`On ...
wrote:`) and drops everything after the cut; the form comes after the
description, so without this a signed description would silently lose the
form, from both what the model reads and the query. The same is done for
pasted text without an activity stream.

In the activity, each entry looks like this on the page:

```
AL                                  avatar initials (optional)
Lovelace, Ada                       author
Additional comments (Customer Viewable Field)•10/21/2026 04:00:00 PM
It died again: ...                  body, up to the next entry's author line
```

`parse_activity(text)` finds every header line matching `_HEADER`
(`<type>•<M/D/YYYY h:mm:ss AM|PM>`, the type 2-80 characters), takes the line
above it as the author, and the lines up to the next entry's author line as
the body (one more line back when that is an initials line, 1-3 capitals).
The time is parsed with `%m/%d/%Y %I:%M:%S %p`, or `None`. Entries come out
in page order: newest first.

## 5.3 `prepare`, step by step

1. **Split** at the first `Work notes / activity:` into head and activity, and
   parse the entries.
   - No label but the text parses into entries (a pasted activity stream):
     the head is the lines before the first header, minus two (the author and
     an initials line).
   - **No entries at all** (text pasted by hand, or an activity stream whose
     headers do not parse): only the cleaning of 5.4 is applied to the whole
     text, plus the forwarded-message rule (5.7), and the query is built from
     the result. Done.
2. **Forwarded message.** `_forwarded_message(head)` (5.7).
3. **Clean the head** with `_clean_text` (5.4); append the forwarded message
   under `(forwarded message, quoted in the email)`.
4. **Find the RC team** (5.5): `_team_addresses`, their local parts, and
   `note_writers`, everyone who wrote a work note.
5. **Walk the entries oldest first** (sorted by time; an entry with no time
   sorts first). For each:

   | entry kind | what happens |
   |---|---|
   | `Email sent` | dropped; `notifications_removed` +1, its addresses counted |
   | `Additional comments` by `System` / `Administrator, System` whose body starts *Thank you for reaching out to Research Computing. We will respond* | dropped; `auto_replies_removed` +1 |
   | `Email received` | the body becomes `Subject: <subject>` (the body is the description already); addresses counted |
   | `Field changes` | the body becomes one line (below); `field_changes_compressed` +1 |
   | anything else (comments, work notes) | the body is cleaned with `_clean_text`; a researcher's non-empty additional comment becomes `latest` |

   Then the author's role (5.5). An entry whose (kind, author, body) was
   already kept is dropped (`duplicates_removed`); ServiceNow sometimes
   renders an entry twice. Each kept entry is rendered (5.6).
6. **Assemble:** the cleaned head, a blank line, `Work notes / activity:
   (oldest first)`, a blank line, the entries separated by blank lines.
7. **Query:** `build_query(head, latest)` (5.8).

`_field_changes(body)` reads the body as pairs of lines, a field name and
`<new>was<old>` (ServiceNow's rendering), and writes
`Incident state: New -> In Progress; Assigned to: Rivera, Sam`, capped at 300
characters. A value with no `was` stays as it is.

`_subject(body)` takes the line after a bare `Subject:`, or the rest of a
`Subject: ...` line.

## 5.4 Cleaning a text: `_clean_text`

Applied to the head and to every comment and work note. It removes lines;
it never changes the words of the lines it keeps.

| order | rule | counts |
|---|---|---|
| 1 | cut everything from the first line that starts a quoted chain: `On ... wrote:`, `--- Original Message ---`, a rule of 10+ underscores, `From: ... Sent: ...` (`_QUOTE_START`) | `quoted_chains_removed` |
| 2 | drop lines starting with `>` | |
| 3 | stop at a line of 5+ dashes (`_RULE`): what follows is a signature | `signatures_removed` |
| 4 | drop lines that are exactly *Show email details*, *Email sent* or *Email received* (case-insensitive) | |
| 5 | `_strip_addresses`: drop `To:`/`Cc:`/`Bcc:`/`From:`/`Reply-To:`/`Sent:` lines that carry an address or nothing (and bare `to:`/`cc:`/`from:`), and lines that are only addresses | `addresses_removed` (per address) |
| 6 | collapse 3+ newlines to one blank line; strip | |

An address inside a sentence stays: the test keeps
`[lovelace.a@explorer-02 ~]$ sbatch run.sh`, a shell prompt with an `@`.

## 5.5 RC staff or researcher

The model has to know who is who. On INC09000008 it did not see that
*Rivera, Sam* was RC, and told the researcher to contact the systems team
that Sam had already contacted.

**The team.** `_team_addresses` takes every *Email sent* notification with at
least `_TEAM_MIN_RECIPIENTS` (3) addresses: a notification to that many people
goes to the RC team, not the researcher. Their addresses are the team; their
local parts (`g.patel`) are `team_locals`.

**Matching a name to an address.** `_is_team_member("Patel, Gita",
team_locals)`: authors are rendered *Last, First*. It is a team member when
some local part containing a dot ends in `.<last>` (spaces removed, lowercase)
and starts with the first letter of the first name: `g.patel` is *Patel,
Gita*, `jor.lee` is *Lee, Jordan*.

**`_role(entry)`**, in order:

| # | condition | role |
|---|---|---|
| 1 | author is `System` or `Administrator, System` | none |
| 2 | the author wrote any work note in this ticket | RC staff (only staff can write work notes) |
| 3 | the author matches a team address (above) | RC staff |
| 4 | the body's first 3 lines have `reply from: <address>` (or `RITM Update: reply from:`) | RC staff if that address is in the team, else researcher |
| 5 | no team was found, or the entry is a *Field changes* | none |
| 6 | otherwise | researcher |

Rule 4 exists because staff mostly comment in ServiceNow itself, so without
a team list an emailed reply is taken as the researcher's. Rule 5: field
changes on a new ticket are made in the caller's name, and so are the IT
Service Desk's, so they say nothing about who is who. `stats.staff_identified`
counts the kept entries labelled RC staff.

## 5.6 What the model reads

`Entry.render()` writes one entry as:

```
[2026-10-22 10:00] Rivera, Sam, RC staff (comment, visible to the researcher):
The job used 31 GB; please request --mem=32G and resubmit.
```

The date is `YYYY-MM-DD HH:MM` (`?` without one); the role is omitted when
unknown. The label in parentheses:

| ServiceNow kind | label |
|---|---|
| starts *Additional comments* | `comment, visible to the researcher` |
| starts *Work notes* | `internal work note` |
| anything else | the kind, lowercased (`field changes`, `email received`) |

So the whole cleaned ticket is the head, then
`Work notes / activity: (oldest first)`, then the entries, latest last. The
prompt (chapter 8) tells the model what the labels mean.

## 5.7 Forwarded messages

A forwarded request (*Copying rchelp.* over the original email) has almost no
words of its own; the request is the quoted message (INC09000011). Normally
the quoted chain is cut (5.4), which would leave nothing to answer.

`_forwarded_message(raw_head)` takes the text after `Description:` (or all of
it), and finds the first `_QUOTE_START` line. If the lines before it, cleaned
and stripped of greeting and sign-off (`_researcher_words`), have fewer than
`_FORWARD_MIN_WORDS` (4) words of two or more letters, the quoted message is
kept: the lines after the quote start, up to the next quote start or dashed
rule, with leading `>` removed, cleaned. It is appended to the head under
`(forwarded message, quoted in the email)` and
`stats.forwarded_message_kept = 1`. The constant's comment gives the
calibration: *Copying rchelp.* has 2 words; *My job 123456 failed with OOM.*
has 5.

## 5.8 The retrieval query: `build_query`

Retrieval searches the researcher's own words, not the thread (5.1).
`build_query(head, latest)` joins, with blank lines, at most
`QUERY_MAX_CHARS` (1,500) characters of:

1. the **Short description**, with any `Re:`, `Fw:`, `Fwd:`, `Subject:`
   prefixes removed;
2. the **Description**, through `_researcher_words`;
3. the **Request form**, as is. A catalog item's answers are the
   researcher's own words (the partition, the PI, *describe your benchmarking
   calculation*), and often all there is;
4. the researcher's **latest** additional comment, through
   `_researcher_words`, unless it is already contained in the description or
   the form.

When the head has none of the three fields (pasted text), the whole head
minus `Ticket:` lines stands in for the description.

`_field(head, name)` reads a labelled field: from `^<name>:` to the next
blank line or the next `Description:` / `Request form:` line. The description
is the exception: its own lines may look like labels
(`srun: Required node not available`), so it runs to the `Request form:` line
or the end of the head. The form runs to the end of the head.

`_researcher_words(text)` removes what only carries names:

- `reply from:` lines and mail footers (*Get Outlook for*, *Sent from my*);
- leading blank lines, greetings (*Hi*, *Hello*, *Hey*, *Dear*, *Good
  morning/afternoon/evening*, as a whole short line) and pleasantries (*Hope
  all is well*, *Hope this email finds ...*);
- everything from a `--` line, or from a sign-off line (*Best*, *Thanks*,
  *Regards*, *Cheers*, ...) once some content has been seen;
- trailing blank lines and a trailing line that is only a name (one to three
  capitalised words).

The model never reads the query; it reads the cleaned ticket.
`main.py` sends `prepared.query or ticket` to retrieval and uses it for
playbook matching too.

> **rc-agent:** `Prepared` has a fifth field, `expansion`:
> `expand(query)` maps symptoms in the query to RC's documentation words
> (`EXPANSIONS`: pending/queued → `queue priority fairshare scheduling`;
> killed/OOM → `memory request --mem out of memory`; quota → `storage quota
> usage`; can't log in/ssh → `ssh login connect`; module/command not found →
> `module load software environment`; idle/low GPU use → `idlebot gpu
> utilization`). It is a second search (`kg_client.retrieve_multi`), not
> appended to the query: appended, the extra words were drowned out by the
> rest of the ticket. The comment's measurement: for *My jobs have been
> pending for days*, *Understanding the Queuing System* went from 13th to 1st
> (2026-10-28). Chapter 16.

## 5.9 Fitting: `fit`

`main.py` computes how many tokens the ticket may take (chapter 4, step 8)
and calls `fit(prepared, budget_tokens * CHARS_PER_TOKEN)`. `CHARS_PER_TOKEN`
is 3.0: English text with commands and IDs runs ~3.5-4 characters per token,
so 3 errs on the safe side; the exact count comes from vLLM afterwards.

| case | result |
|---|---|
| the text fits | unchanged; `entries_omitted: 0` |
| no `Work notes / activity: (oldest first)` marker | the beginning, which holds the request, cut to the budget, then `[... truncated to fit the model's context ...]`; `truncated_chars` |
| a thread | the head and the **first** entry are always kept; then the **newest** entries, from the end, while they fit in what is left (minus 120 characters of slack); between them `[... N earlier activity entries omitted to fit the model's context ...]`; `entries_omitted` |
| still too long (one entry alone exceeds the budget) | the result is cut to the budget with the truncation marker; `truncated_chars` |

Entries are split on a blank line followed by `[YYYY-MM-DD HH:MM] ` or `[?] `,
which is how 5.6 renders them. The query is passed through unchanged.

## 5.10 Stats

`Prepared.stats` goes to the run log (`trace.ticket_context`), the
`ticket_context.prepare` run, and the response's `context`, which the panel
summarises under *Ticket & model details*.

| key | from | meaning |
|---|---|---|
| `addresses_removed` | prepare | email addresses removed (lines, lists, notifications, received emails) |
| `notifications_removed` | prepare | *Email sent* entries dropped |
| `auto_replies_removed` | prepare | ServiceNow acknowledgements dropped |
| `field_changes_compressed` | prepare | *Field changes* entries made one line |
| `signatures_removed` | prepare | texts cut at a dashed rule |
| `quoted_chains_removed` | prepare | texts cut at a quoted chain |
| `duplicates_removed` | prepare | repeated entries dropped |
| `entries_kept` | prepare | entries in the output |
| `staff_identified` | prepare | kept entries labelled RC staff (only when there is an activity stream) |
| `forwarded_message_kept` | prepare | 1 when a forwarded message was kept |
| `budget_chars` | fit | the budget |
| `entries_omitted` | fit | middle entries left out |
| `truncated_chars` | fit | characters cut when thinning was not enough |

`main.py` adds `raw_chars`, `chars`, `context_window`, `budget_tokens`,
`retrieval_query_chars` and `playbooks`.

## 5.11 Tests

`backend/.venv/bin/python tests/ticket_context/test_ticket_context.py`
(exit 0 = pass) runs on a synthetic ticket in the exact shape the extension
scrapes, with no real people:

| section | proves |
|---|---|
| cleaning | team addresses and To/Cc/From lines gone and counted; notifications, UI chrome, initials, signature and quoted chain gone; a shell prompt with `@` kept; every message kept; field changes on one line; email received as its subject; a duplicate once; visibility labels; oldest first; description before the thread; much shorter |
| pasted text | without an activity stream only the address line and the quote go |
| fitting | fits the budget; says what was left out; keeps the description, the first and the newest entry; no change when it fits; the query survives |
| who is who | a work-note writer is RC staff everywhere; the caller is the researcher; team addresses (`s.rivera`, `jor.lee`) name staff; an emailed reply from outside is the researcher's; the acknowledgement goes; the query has the subject without `Re:`, a label-like description line, the latest researcher message, and no staff replies, greetings, sign-offs, names or phone numbers |
| request form | the form stays in the text and is in the query; with a description too, both, the form once; a signature or quoted chain in the description does not cut off the form, with or without an activity stream |
| forwarded requests | a forward with no words of its own keeps the quoted request, and it is searched; its signature still goes; an ordinary reply still drops its quoted chain |

> **rc-agent:** the test adds a *query expansion* section: pending and OOM
> map to their words, a question with no symptom expands to nothing, and the
> expansion never enters the query.
