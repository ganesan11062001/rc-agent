---
name: 6. Cluster lookups
route: /lookups
menu: The pipeline
---

# 6. Cluster lookups

Before drafting, the backend checks what is true on the cluster **now**
about what the ticket names: does this account exist and which groups is it
in, what happened to that job, who is in that group, is that node down. The
draft then states facts instead of guessing, and the engineer sees exactly
which commands ran and what they said.

All of it is `ticketmate/backend/app/lookups.py`. It imports only the
standard library.

| function | role |
|---|---|
| `find_candidates(ticket_text)` | the validated targets the ticket names, by kind, and whether the researcher says a job is waiting or failing |
| `run(ticket_text, timeout)` | every lookup the ticket calls for, run concurrently; never raises; returns `List[Lookup]` |
| `prompt_text(lookups)` | the lookups as the prompt shows them |

A `Lookup` is `kind`, `target`, `command` (as displayed), `ok`, `summary`
(one line, for the panel), `output` (what the prompt gets, capped) and `ms`.
`to_dict()` gives the shape of `models.ClusterLookup`.

## 6.1 The threat, and the rules

The lookups run inside the RC engineer's own Slurm job, **as the engineer**,
on text a researcher wrote. The module docstring sets the rules, and they are
enforced in this module, not left to the model, which never chooses a
command:

| rule | how |
|---|---|
| lookups only | the only programs ever run are `id`, `getent`, `sacct`, `scontrol`, `sinfo`, `squeue`, `sshare`, `stat` and `ldconfig`, each with a fixed argument list; plus reading one file (`group.xtra`). Nothing that changes, updates or deletes anything is reachable from the module |
| no shell | `asyncio.create_subprocess_exec(*argv)`: `;`, `$( )`, backticks, quotes and pipes in a ticket are just characters |
| strict arguments | every argument taken from the ticket must **fully** match a pattern (`re.fullmatch`), or it is dropped |
| no email addresses as usernames | every RC team member is CC'd on most tickets; looking them all up would be noise |
| no secrets | the child process gets a minimal environment, not the backend's (6.5) |
| bounded | at most a few targets per kind, a timeout per command, capped output |
| only when relevant | queue position and FairShare only when the researcher says a job is waiting; recent failures only when they say jobs fail |

## 6.2 Finding targets

`find_candidates` scans the **cleaned** ticket (`prepared.text`, chapter 5),
so notification address lists are already gone. Each kind has a candidate
pattern that finds it in context, then a strict pattern it must fully match.
Targets keep their order of appearance, without repeats, up to a cap.

| kind | found by | must fully match | cap |
|---|---|---|---|
| user | a standalone lowercase `word.word`, not part of an email, path, URL or domain (`_USER_CANDIDATE`); not ending in a file or domain suffix (`sh`, `py`, `out`, `edu`, `com`, `json`, `sbatch`, ... `_NOT_USER_SUFFIXES`); not `e.g` / `i.e` | `USERNAME` `[a-z][a-z0-9-]{0,30}\.[a-z][a-z0-9-]{0,30}` (Explorer usernames are `<last>.<initial>` or `<initial>.<last>`) | `MAX_USERS` 4 |
| job | `job 4812345`, `job id: ...`, `job #...`, `jobid=...`, `slurm-4812345.out` (and `_N` array suffixes), `batch job ...`, `sacct -j ...`, `seff ...`, `scontrol show job ...`; ordered by position across all patterns | `JOB_ID` `[1-9][0-9]{4,9}` | `MAX_JOBS` 3 |
| group | `<name> group` or `<name> unix group`, lowercased; not a common word or a ServiceNow label (`the`, `your`, `research`, `access`, `assignment`, `support`, ... `_NOT_GROUPS`) | `GROUP` `[a-z][a-z0-9_-]{2,31}` | `MAX_GROUPS` 2 |
| node | a standalone `c0602`, `d4043`: not in a path, file name or hostname (`explorer-02`, `x.c0602`, `c0602.out` do not count; `UnavailableNodes:d4043` does) | `NODE` `[a-z]{1,2}[0-9]{4}` | `MAX_NODES` 3 |
| partition | `<name> partition`; `partition: <name>`; `-p <name>` / `--partition=<name>`; `nodes on|in [the] <a> [and|,|& <b>]`; lowercased; not a common word (`_NOT_PARTITIONS`) and not a node name | `PARTITION` `[a-z][a-z0-9_-]{1,31}` | `MAX_PARTITIONS` 2 |
| project | `/projects/<name>` | `PROJECT` `[A-Za-z0-9][A-Za-z0-9_-]{0,63}` | `MAX_PROJECTS` 2 |
| library | `libcudnn.so.8` and the like (`error while loading shared libraries: ...`) | `LIBRARY` `lib[A-Za-z0-9_+-]{1,60}\.so(\.[0-9]{1,5}){0,4}` | `MAX_LIBRARIES` 3 |

Two flags decide the per-user lookups:

| flag | set when the ticket matches (case-insensitive) |
|---|---|
| `waiting` | *pending*, *in (the) queue*, *queued*, *waiting*, *wait(ed/ing) for / so long / days / hours*, *not (yet) started/starting*, *hasn't started*, *start time*, *fairshare*, *priority*, `(Priority)`, `(Resources)`, `PD` |
| `failing` | *fail(ed/s/ing/ure)*, *crash(ed/es/ing)*, *killed*, *died*, `OUT_OF_MEMORY`, `OOM`, `NODE_FAIL`, `TIMEOUT`, *cancel(l)ed*, *exit code*, *segfault*, *error* |

A ticket number is not a job ID: a bare number needs a *job* context to be
found. With no target of any kind, `run` returns `[]` and runs nothing.

## 6.3 The commands

Each kind runs only if its program is on `PATH` (`shutil.which`), so a
machine without Slurm simply has fewer lookups. `ldconfig` is checked by path
instead: `RC_LDCONFIG`, default `/sbin/ldconfig`.

| kind | runs when | exact argv | shown as | summary |
|---|---|---|---|---|
| `user` | a username was found | `id <user>` | `id <user>` | *account exists; N groups: a, b, ...* (first 12 names), or *no account named <user>* |
| `job` | a job ID was found | `sacct -j <id> --parsable2 --format=JobID,JobName%30,User,Account,Partition,State,ExitCode,Elapsed,Timelimit,ReqMem,MaxRSS,AllocCPUS,NodeList,Start,End,WorkDir` | `sacct -j <id>` | *<State>, exit <ExitCode>, ran <Elapsed> of <Timelimit>, <ReqMem> requested, max RSS <x>, <Partition> on <NodeList>*; or *no record of job <id>* |
| `script` | for the first 2 jobs found (`MAX_SCRIPTS`) | `sacct -j <id> --batch-script` | the same | *N #SBATCH lines* |
| `queue` (job) | a job found PENDING | `squeue -h -j <id> --start -o %i\|%S\|%r` | `squeue -j <id> --start` | *expected start <time> (reason: <r>)* or *no start time estimated yet (reason: <r>)* |
| `queue` (job) | a job found RUNNING | `squeue -h -j <id> -O TimeLeft` | `squeue -j <id> -O TimeLeft` | *time left <t>* |
| `group` | a group name was found | `getent group <name>`, and a read of `group.xtra` | `getent group <name>` | *N members; includes <users>; does not list <users>*, or *no group named <name>* |
| `node` | a node name was found | `scontrol show node <name>` | the same | *<State>, partitions <P>, reason: <R>*, or *no node named <name>* |
| `partition` | a partition name was found | `sinfo -h -N -p <name> -o %N\|%T\|%E` | `sinfo -N -p <name>` | *N nodes: 3 idle, 1 down*, ... |
| `project` | a `/projects/<name>` was found | `stat -c %U\|%G /projects/<name>` | the same | */projects/<name> is owned by <user>, group <group>* |
| `queue` (user) | `waiting`, for up to 2 existing users (`MAX_ACTIVITY_USERS`) | `squeue -h -u <user> -o %i\|%P\|%j\|%T\|%S\|%L\|%r` | `squeue -u <user> --start` | *N jobs: 2 PENDING, 1 RUNNING* or *no jobs in the queue* |
| `fairshare` | `waiting`, for up to 2 existing users | `sshare -a -P -u <user>`, then `sshare -a -P -A <account>` | `sshare -a -u <user>` | *FairShare <f>, group <account>*, with *(below 0.5: expect long waits)* when it is |
| `history` | `failing`, for up to 2 existing users | `sacct -u <user> -S now-7days -X -P -o JobID,JobName%30,Partition,State,ExitCode,NodeList,End` | `sacct -u <user> -S now-7days -X` | *last 7 days, N jobs: 1 FAILED, 1 OUT_OF_MEMORY, ...* |
| `library` | a library name was found | `/sbin/ldconfig -p`, once for all names, filtered in Python | `ldconfig -p \| grep <stem>.so` | *<lib> is in the system library cache*; *... is not, but other versions are: ...*; *... is not in the system library cache (it may come from a module)* |

Notes on individual commands:

- **`group.xtra`.** RC-managed groups are not in `/etc/group`. Their members
  are in `RC_GROUP_XTRA` (default `/shared/admin/ROOT/etc/group.xtra`),
  lines of `name:x:gid:member1,member2`, read as a file. A group found in
  either source counts.
- **Group membership is reported only for accounts that exist**: `run` looks
  up users first and passes `_group` only the ones `id` found, so a token
  that merely looked like a username is not reported as *not a member*.
- **FairShare** is per group: one member's heavy use slows everyone in it.
  The second `sshare` reads the user's account (validated against
  `[A-Za-z0-9_.-]{1,64}` before use, though it came from `sshare` itself) and
  adds *group <account>: N users* and *the heaviest user accounts for X% of
  the group's usage*, marked *(this user)* when it is them. Other members are
  counted, never named.
- **Projects.** An owner of `nobody`, `UNKNOWN` or a number means NFS without
  id mapping on this node: the lookup is marked failed (*owner ... not
  visible from this node*), not reported as the owner.
- **Libraries.** `ldconfig -p` only says what this node has; the docstring
  notes that compute nodes share its image.
- **The displayed command is a short form** for the engineer, not always the
  exact argv (above). The `squeue -u` line says `--start`; the argv does not
  use it but asks for the start time column (`%S`).

## 6.4 Order of execution

`run(ticket_text, timeout)`:

1. `find_candidates`; nothing found → `[]`.
2. **Users**, concurrently (`asyncio.gather`). `known` = the users that exist.
3. **Everything else that needs no job state**, concurrently: jobs, groups
   (with `known`), nodes, partitions, projects, and, for the first two
   `known` users, queue and FairShare when `waiting` and history when
   `failing`.
4. **Libraries**: one `ldconfig -p`.
5. **Follow-ups that need a job's state**, concurrently: the batch script of
   the first two jobs found, and the queue position or time left of every job
   found (from the state in its summary).
6. **Filter**: failed `partition`, `project` and `script` lookups are dropped.
   A word that only looked like a partition or project name, or a job with no
   stored script, is not worth a line. Failed `user`, `job`, `group` and
   `node` lookups stay: *no account named ...* is information.

Any exception inside is logged and `run` returns `[]`: a lookup never breaks
a draft. In `main.py` the whole of `run` is an asyncio task started before
knowledge-graph retrieval and awaited after it (chapter 4), inside a
`cluster.lookups` run of the trace with metadata `read_only: true`. It is
skipped when `CLUSTER_LOOKUPS=false`.

## 6.5 How each command is run

`_run(argv, timeout)`:

| aspect | value |
|---|---|
| exec | `asyncio.create_subprocess_exec(*argv)`, never a shell |
| stdin | `/dev/null` |
| stdout, stderr | captured together |
| environment | only `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `SLURM_CONF`, `TZ` from the backend's environment; `LANG` defaults to `C`. The commands need the path and the Slurm configuration, not the backend's tokens (`AUTH_TOKEN`, `RC_KG_API_TOKEN`) |
| timeout | `CLUSTER_LOOKUP_TIMEOUT_SECONDS` (default 5.0) per command, via `asyncio.wait_for`; on expiry the process is killed and reaped, and the lookup says *timed out after Ns* |
| output | decoded as UTF-8 (invalid bytes replaced), stripped, cut to `MAX_OUTPUT_CHARS` (2,000) with `... (truncated)` |
| cannot start | *could not run: ...* |
| returns | `(exit code or None on timeout, text, milliseconds)` |

There is no deadline for `run` as a whole. Each phase of 6.4 waits for its
slowest command, and `_fairshare` runs its two `sshare` calls one after the
other, so with every command hanging a draft could wait up to five timeouts
(phases 2, 3 twice, 4, 5) before retrieval's result is used.

## 6.6 Output shaping

Raw command output is long and mostly noise for a prompt. Each lookup keeps
what a reply needs:

| kind | `output` (what the prompt gets) |
|---|---|
| `user` | `id`'s line |
| `job` | the header and up to 9 rows of `sacct` (steps included); the summary takes the largest MaxRSS of all steps, converted from sacct's binary K/M/G to `59.6G` form |
| `script` | the script, up to `MAX_SCRIPT_CHARS` (1,200) |
| `group` | the summary only: the member list can run to hundreds of names, and the prompt needs its size and whether the ticket's users are in it |
| `node` | only `NodeName`, `State`, `Partitions`, `Gres`, `CPUAlloc`, `CPUTot`, `RealMemory`, `AllocMem`, `Reason`, `BootTime`, `LastBusyTime` (`_NODE_FIELDS`); sockets, kernel and energy counters are noise |
| `partition` | counts by state over distinct nodes (a node in two partitions is listed twice by `sinfo`), then only the nodes whose state matches *down, drain, fail, maint, not_responding, unknown, inval*, with Slurm's reason, up to 15 |
| `queue` (user) | the summary, a header line and up to 12 jobs |
| `fairshare` | the summary and the group lines (no names) |
| `history` | the summary and up to the 10 newest jobs that are not COMPLETED, RUNNING or PENDING |
| `project`, `queue` (job) | the summary |
| `library` | the summary and up to 6 matching cache lines |

## 6.7 How results reach the prompt, the panel and the trace

**Prompt.** `prompt_text(lookups)` writes each lookup as a command line and
its result, separated by blank lines:

```
$ id doe.l
uid=1234(doe.l) gid=100(users) groups=100(users),5001(gaussian),6002(smith_lab)

$ sacct -j 4812345
JobID|JobName|...
```

The result is `output` for a successful lookup, and `summary` for a failed
one and for groups. `prompts.build_messages` puts it right after the ticket,
because these are facts about it, inside `CLUSTER_LOOKUPS_TEMPLATE`:

```
--- CLUSTER LOOKUPS ---
Read-only commands RC Copilot ran on the cluster just now, ...
where they disagree with the ticket ..., trust them. ...
Do not paste raw command output into "draft_response", and do not claim
anything they do not show. A "no account" result may simply mean the word
was not a username.

<prompt_text>
--- END CLUSTER LOOKUPS ---
```

With lookups present the checklist adds *Use what the CLUSTER LOOKUPS show
... do not ask anyone to run a lookup that was already run*. The system
prompt lets the model say something was done (a user added, a node fixed)
only when an RC staff entry or the lookups show it, and for faults in the
cluster itself tells it to say what the lookups show rather than give the
researcher diagnostic commands. With no lookups there is no block. The
lookups text is counted in the ticket's token budget (chapter 4, step 8).
Chapter 8 covers the prompt.

**Response and panel.** `DraftResponse.lookups` carries every lookup
(`kind`, `target`, `command`, `ok`, `summary`, `output`, `ms`). The panel's
*Cluster lookups (N ran)* section lists each `command` and `summary`, marked
ok or not, with *Read-only, run just now in your session. Nothing was
changed.*

**Trace.** The run log's `trace.cluster_lookups` has every lookup in full;
the root run is tagged `lookups:<n>`; `timings_ms.lookups` is the wall time;
the metrics row counts them.

> **rc-agent:** the agents' tools call the same functions directly
> (`lookups._user`, `_job`, `_group`, `_node`, `_partition`, `_user_queue`,
> `_fairshare`, `_history`, `_project_owner`, `_libraries`), with arguments a
> model chose, not ones `find_candidates` validated. So each of those
> functions checks its own argument first: `_invalid(kind, value, pattern)`
> returns a `Lookup` with command `(not run)` and summary *refused: '<value>'
> is not a valid <kind> argument* unless the value fully matches the same
> pattern. The refusal is what the agent sees. Chapter 15.

## 6.8 Tests

`backend/.venv/bin/python tests/lookups/test_lookups.py` (exit 0 = pass)
needs nothing from the cluster. It puts fake `id`, `getent`, `sacct`,
`scontrol`, `sinfo`, `squeue`, `sshare`, `stat` and `ldconfig` first on
`PATH` (and points `RC_GROUP_XTRA` and `RC_LDCONFIG` at test files). Each
fake records its argv, so the test proves what ran.

| section | proves |
|---|---|
| candidates | the username is found; emails are never usernames; file names and paths are not; lookups are capped; job IDs in order of appearance; a ticket number is not a job ID; the group is found; every argument fully matches its pattern |
| running | finishes promptly; **only** the read-only programs ran (`READ_ONLY`); every argv has exactly the allowed shape (`shape_ok`); the ticket's `;touch`, `$(touch ...)`, backticks, quotes and `\|touch` executed nothing (a marker file never appears); account, job (state, max RSS, requested memory), group (count, membership, no other members passed on, only existing accounts); unknown users said so |
| nodes and partitions | node names, not hostnames, files or paths; partitions from *nodes on X and Y*; a down node with its reason and only the useful fields; an unknown node said so; a partition's counts and its bad nodes; a word that is not a partition leaves no line |
| queue, FairShare, history, scripts, projects, libraries | a batch script shown, a missing one leaves no line; recent failures when jobs fail; no queue or FairShare when nothing waits; no library lookup when none is named; queue counts and FairShare with group usage when waiting; project owner, and no line for an owner shown as `nobody`; other library versions named; a group only in `group.xtra` |
| timeouts and missing commands | a hanging command is cut off; no `sacct` on `PATH`: job lookups skipped, no error; nothing to look up: nothing runs |
| prompt | the results appear in the prompt's `CLUSTER LOOKUPS` block as commands and output; no lookups, no block |
