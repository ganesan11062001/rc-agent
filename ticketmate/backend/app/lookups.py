"""Read-only cluster lookups for the names and job IDs a ticket mentions.

Before drafting, the backend can check what is true on the cluster *now* --
does this account exist and which groups is it in, what happened to that job,
who is in that group -- so the draft states facts instead of guessing. The
results go into the prompt and are returned with the draft, so the RC member
sees exactly which commands ran and what they said.

These run inside the RC member's own Slurm job, as them, on text a researcher
wrote. So the rules are strict, and enforced here rather than trusted to the
model, which never chooses a command:

- Lookups only. The only programs ever run are `id`, `getent group`, `sacct`,
  `scontrol show node`, `sinfo`, `squeue`, `sshare`, `stat` and `ldconfig -p`,
  each with a fixed argument list (plus reading the group.xtra file). Nothing
  that changes, updates or deletes anything is reachable from this module.
  Those that only matter for some tickets run only then: queue position and
  FairShare when the researcher says a job is waiting, recent failures when
  they say jobs fail.
- No shell. Commands are exec'd with an argv list, so `;`, `$( )`, backticks
  or quotes in a ticket are just characters.
- Every argument taken from the ticket must fully match a strict pattern
  (`doe.l`, `4812345`, `gaussian`), or it is dropped.
- Email addresses are never treated as usernames: every RC team member is
  CC'd on most tickets, and looking them all up would be noise.
- Bounded: a few lookups per draft, a timeout per command, capped output.
"""

import asyncio
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

MAX_USERS = 4
MAX_JOBS = 3
MAX_GROUPS = 2
MAX_NODES = 3
MAX_PARTITIONS = 2
MAX_PROJECTS = 2
MAX_LIBRARIES = 3
MAX_ACTIVITY_USERS = 2     # users whose queue, FairShare and recent jobs are looked up
MAX_SCRIPTS = 2            # job scripts shown (sacct --batch-script)
MAX_SCRIPT_CHARS = 1200
MAX_OUTPUT_CHARS = 2000
# Groups not in /etc/group (RC-managed): "name:x:gid:member1,member2".
GROUP_XTRA = os.environ.get("RC_GROUP_XTRA", "/shared/admin/ROOT/etc/group.xtra")

# What an argument must look like, in full, before it is passed to a command.
# Explorer usernames are `<last>.<initial>` or `<initial>.<last>`.
USERNAME = re.compile(r"[a-z][a-z0-9-]{0,30}\.[a-z][a-z0-9-]{0,30}")
JOB_ID = re.compile(r"[1-9][0-9]{4,9}")
GROUP = re.compile(r"[a-z][a-z0-9_-]{2,31}")
# Explorer's compute nodes: c0602, d4043.
NODE = re.compile(r"[a-z]{1,2}[0-9]{4}")
PARTITION = re.compile(r"[a-z][a-z0-9_-]{1,31}")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
LIBRARY = re.compile(r"lib[A-Za-z0-9_+-]{1,60}\.so(?:\.[0-9]{1,5}){0,4}")

# Tokens shaped like a username that are really file names or abbreviations.
_NOT_USER_SUFFIXES = {
    "sh", "py", "out", "err", "txt", "log", "md", "csv", "tsv", "json", "yaml", "yml",
    "edu", "com", "org", "net", "html", "htm", "pdf", "png", "jpg", "sbatch", "slurm",
    "r", "m", "c", "h", "cpp", "ipynb", "gz", "tar", "zip", "so", "sif", "conf", "cfg",
    "ini", "env", "e", "g", "x", "lock", "bashrc", "img", "mat", "nc", "h5", "hdf5",
}
_NOT_GROUPS = {"the", "your", "my", "our", "their", "this", "that", "research", "user", "users",
               "unix", "linux", "slurm", "access", "a", "an", "same", "new", "following", "group",
               # ServiceNow's own field labels ("Assignment group", "Support group").
               "assignment", "support", "resolver", "approval", "watch"}

# A username: a standalone token, not part of an email, path, URL or domain.
_USER_CANDIDATE = re.compile(r"(?<![\w@./-])([a-z][a-z0-9-]*\.[a-z][a-z0-9-]*)(?![\w@-]|\.[a-z0-9])")
_JOB_CANDIDATES = [
    re.compile(r"\bjob(?:\s*id)?\s*(?:#|:|=|number)?\s*([1-9][0-9]{4,9})\b", re.I),
    re.compile(r"\bjobid\s*[:=]?\s*([1-9][0-9]{4,9})\b", re.I),
    re.compile(r"\bslurm-([1-9][0-9]{4,9})(?:_\d+)?\.out\b"),
    re.compile(r"\bbatch job ([1-9][0-9]{4,9})\b", re.I),
    re.compile(r"\b(?:sacct|seff|scontrol show job)\s+(?:-j\s*)?([1-9][0-9]{4,9})\b"),
]
_GROUP_CANDIDATE = re.compile(r"\b([A-Za-z][A-Za-z0-9_-]{2,31})\s+(?:unix\s+)?group\b")
# "UnavailableNodes:d4043" counts; "explorer-02", "x.c0602" and "c0602.out" do not.
_NODE_CANDIDATE = re.compile(r"(?<![\w./@-])([a-z]{1,2}[0-9]{4})(?![\w@/-]|\.[a-z0-9])")
_PARTITION_CANDIDATES = [
    re.compile(r"\b([a-z][a-z0-9_-]{1,31})\s+partition\b", re.I),
    re.compile(r"\bpartition\s*[:=]?\s*([a-z][a-z0-9_-]{1,31})\b"),
    re.compile(r"(?:\s-p\s*|--partition[= ])([a-z][a-z0-9_-]{1,31})\b"),
    # "the nodes on ce-mri and ai-jumpstart are down"
    re.compile(r"\bnodes\s+(?:on|in)\s+(?:the\s+)?([a-z][a-z0-9_-]{1,31})(?:\s*(?:,|and|&)\s*([a-z][a-z0-9_-]{1,31}))?"),
]
_PROJECT_CANDIDATE = re.compile(r"(?<![\w.])/projects/([A-Za-z0-9][A-Za-z0-9_-]{0,63})(?![\w-])")
# "error while loading shared libraries: libcudnn.so.8"
_LIBRARY_CANDIDATE = re.compile(r"(?<![\w/.+-])(lib[A-Za-z0-9_+-]{1,60}\.so(?:\.[0-9]{1,5}){0,4})(?![\w+-])")
# What the researcher says decides which per-user lookups are worth running.
_WAITING = re.compile(r"(?i)\b(pending|in (the )?queue|queued|waiting|wait(ed|ing)? (for|so long|days|hours)|"
                      r"not (yet )?start(ed|ing)|hasn'?t started|start time|fair ?share|priority|"
                      r"\(Priority\)|\(Resources\)|PD)\b")
_FAILING = re.compile(r"(?i)\b(fail(ed|s|ing|ure)?|crash(ed|es|ing)?|killed|died|OUT_OF_MEMORY|OOM|"
                      r"NODE_FAIL|TIMEOUT|cancel+ed|exit code|segfault|error)\b")
_NOT_PARTITIONS = _NOT_GROUPS | {"this", "that", "which", "each", "every", "specific", "default", "any",
                                 "slurm", "settings", "name", "names", "list", "is", "was", "nodes", "node", "jobid",
                                 "having", "has", "have", "are", "were", "with", "for", "and", "to", "on",
                                 "in", "of", "not", "request", "requests", "limits", "limit", "time", "info"}


@dataclass
class Lookup:
    """One command that ran (or was skipped), for the prompt, the UI and the run log."""
    kind: str                     # "user" | "job" | "group" | "node" | "partition"
    target: str
    command: str                  # as displayed, e.g. "id doe.l"
    ok: bool
    summary: str                  # one line for the panel
    output: str = ""              # what the command printed (capped)
    ms: int = 0

    def to_dict(self) -> Dict[str, object]:
        """The lookup as a JSON-ready dict (also the shape of models.ClusterLookup)."""
        return {"kind": self.kind, "target": self.target, "command": self.command, "ok": self.ok,
                "summary": self.summary, "output": self.output, "ms": self.ms}


@dataclass
class Candidates:
    """The validated lookup targets found in one ticket, by kind, each list capped."""
    users: List[str] = field(default_factory=list)
    jobs: List[str] = field(default_factory=list)
    groups: List[str] = field(default_factory=list)
    nodes: List[str] = field(default_factory=list)
    partitions: List[str] = field(default_factory=list)
    projects: List[str] = field(default_factory=list)
    libraries: List[str] = field(default_factory=list)
    waiting: bool = False         # the researcher says a job is pending / not starting
    failing: bool = False         # ... that jobs fail, crash or are killed


def _first(seen: List[str], value: str, pattern: "re.Pattern[str]", limit: int) -> None:
    if value not in seen and len(seen) < limit and pattern.fullmatch(value):
        seen.append(value)


def find_candidates(ticket_text: str) -> Candidates:
    """Usernames, job IDs, and group, node and partition names the ticket
    mentions, in order of appearance, each already validated against its
    strict pattern."""
    c = Candidates()
    for m in _USER_CANDIDATE.finditer(ticket_text):
        token = m.group(1)
        if token.rsplit(".", 1)[1] in _NOT_USER_SUFFIXES or token in {"e.g", "i.e"}:
            continue
        _first(c.users, token, USERNAME, MAX_USERS)
    # In order of appearance across all the job patterns, not pattern by pattern.
    found = sorted((m.start(1), m.group(1)) for p in _JOB_CANDIDATES for m in p.finditer(ticket_text))
    for _, job in found:
        _first(c.jobs, job, JOB_ID, MAX_JOBS)
    for m in _GROUP_CANDIDATE.finditer(ticket_text):
        name = m.group(1).lower()
        if name not in _NOT_GROUPS:
            _first(c.groups, name, GROUP, MAX_GROUPS)
    for m in _NODE_CANDIDATE.finditer(ticket_text):
        _first(c.nodes, m.group(1), NODE, MAX_NODES)
    found = sorted((m.start(), g.lower()) for p in _PARTITION_CANDIDATES for m in p.finditer(ticket_text)
                   for g in m.groups() if g)
    for _, name in found:
        if name not in _NOT_PARTITIONS and not NODE.fullmatch(name):
            _first(c.partitions, name, PARTITION, MAX_PARTITIONS)
    for m in _PROJECT_CANDIDATE.finditer(ticket_text):
        _first(c.projects, m.group(1), PROJECT, MAX_PROJECTS)
    for m in _LIBRARY_CANDIDATE.finditer(ticket_text):
        _first(c.libraries, m.group(1), LIBRARY, MAX_LIBRARIES)
    c.waiting = bool(_WAITING.search(ticket_text))
    c.failing = bool(_FAILING.search(ticket_text))
    return c


def _env() -> Dict[str, str]:
    """A minimal environment: the commands need PATH and the Slurm config, not
    the backend's tokens."""
    keep = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "SLURM_CONF", "TZ")
    env = {k: os.environ[k] for k in keep if k in os.environ}
    env.setdefault("LANG", "C")
    return env


async def _run(argv: List[str], timeout: float) -> "tuple[Optional[int], str, int]":
    """Exec argv (never through a shell); return (exit code or None on
    timeout, combined output capped, milliseconds)."""
    t = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT, env=_env())
    except OSError as exc:
        return None, "could not run: %s" % exc, int((time.perf_counter() - t) * 1000)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return None, "timed out after %.0fs" % timeout, int((time.perf_counter() - t) * 1000)
    text = out.decode("utf-8", "replace").strip()
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n... (truncated)"
    return proc.returncode, text, int((time.perf_counter() - t) * 1000)


async def _user(name: str, timeout: float) -> Lookup:
    """`id <name>`: whether the account exists and which groups it is in."""
    argv = ["id", name]
    rc, out, ms = await _run(argv, timeout)
    if rc == 0:
        groups = re.search(r"groups=(.*)$", out)
        names = re.findall(r"\(([^)]+)\)", groups.group(1)) if groups else []
        summary = "account exists; %d groups%s" % (
            len(names), (": " + ", ".join(names[:12]) + (" ..." if len(names) > 12 else "")) if names else "")
        return Lookup("user", name, " ".join(argv), True, summary, out, ms)
    summary = "no account named %s" % name if rc is not None and "no such user" in out.lower() else out
    return Lookup("user", name, " ".join(argv), False, summary, out, ms)


_SACCT_FIELDS = "JobID,JobName%30,User,Account,Partition,State,ExitCode,Elapsed,Timelimit,ReqMem,MaxRSS,AllocCPUS,NodeList,Start,End,WorkDir"


async def _job(job_id: str, timeout: float) -> Lookup:
    """`sacct -j <id>`: the job's state, exit code, run time, memory and nodes."""
    argv = ["sacct", "-j", job_id, "--parsable2", "--format=" + _SACCT_FIELDS]
    rc, out, ms = await _run(argv, timeout)
    shown = "sacct -j %s" % job_id
    lines = [l for l in out.splitlines() if l.strip()]
    if rc != 0 or len(lines) < 2:
        return Lookup("job", job_id, shown, False,
                      "no record of job %s" % job_id if rc == 0 else (out or "sacct failed"), out, ms)
    head = lines[0].split("|")
    rows = [dict(zip(head, l.split("|"))) for l in lines[1:]]
    job = rows[0]
    rss = [r.get("MaxRSS") for r in rows if r.get("MaxRSS")]
    summary = "%s, exit %s, ran %s of %s, %s requested%s, %s on %s" % (
        job.get("State", "?"), job.get("ExitCode", "?"), job.get("Elapsed", "?"), job.get("Timelimit", "?"),
        job.get("ReqMem", "?"), (", max RSS " + _human(max(rss, key=_mem_bytes))) if rss else "",
        job.get("Partition", "?"), job.get("NodeList", "?"))
    return Lookup("job", job_id, shown, True, summary, "\n".join(lines[:10]), ms)


def _human(value: str) -> str:
    """sacct's MaxRSS ("62489320K") as people write it ("59.6G")."""
    b = _mem_bytes(value)
    for unit, size in (("T", 1024 ** 4), ("G", 1024 ** 3), ("M", 1024 ** 2)):
        if b >= size:
            return "%.1f%s" % (b / size, unit)
    return value


def _mem_bytes(value: str) -> float:
    """A sacct memory value ("62489320K") in bytes; 0.0 if it does not parse."""
    m = re.fullmatch(r"([\d.]+)([KMGT]?)", value or "")
    if not m:
        return 0.0
    # sacct's K/M/G are binary (KiB, ...).
    return float(m.group(1)) * {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}[m.group(2)]


async def _group(name: str, users: List[str], timeout: float) -> Lookup:
    """`getent group <name>`: member count, and which of the ticket's known users
    are or are not listed in it.
    """
    argv = ["getent", "group", name]
    rc, out, ms = await _run(argv, timeout)
    members = [m for m in out.split(":")[-1].split(",") if m] if rc == 0 and out else []
    # RC-managed groups are also listed in group.xtra; a file read, not a command.
    xtra = _xtra_members(name)
    if xtra is not None:
        members += [m for m in xtra if m not in members]
    if (rc != 0 or not out) and xtra is None:
        return Lookup("group", name, " ".join(argv), False, "no group named %s" % name, "", ms)
    # The member list itself can run to hundreds of names; the prompt only
    # needs its size and whether the ticket's users are in it.
    inside = [u for u in users if u in members]
    outside = [u for u in users if u not in members]
    summary = "%d members" % len(members)
    if inside:
        summary += "; includes " + ", ".join(inside)
    if outside:
        summary += "; does not list " + ", ".join(outside)
    return Lookup("group", name, " ".join(argv), True, summary, summary, ms)


def _xtra_members(name: str) -> Optional[List[str]]:
    """Members of `name` in GROUP_XTRA, or None if it is not listed there."""
    try:
        with open(GROUP_XTRA, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith(name + ":"):
                    return [m for m in line.rstrip("\n").split(":")[-1].split(",") if m]
    except OSError:
        pass
    return None


async def _job_script(job_id: str, timeout: float) -> Lookup:
    """`sacct -j <id> --batch-script`: the #SBATCH lines and the commands the job ran."""
    argv = ["sacct", "-j", job_id, "--batch-script"]
    rc, out, ms = await _run(argv, timeout)
    if rc != 0 or "#!" not in out and "#SBATCH" not in out:
        return Lookup("script", job_id, " ".join(argv), False, "no batch script stored for job %s" % job_id, "", ms)
    text = out if len(out) <= MAX_SCRIPT_CHARS else out[:MAX_SCRIPT_CHARS] + "\n... (truncated)"
    sbatch = [l.strip() for l in out.splitlines() if l.strip().startswith("#SBATCH")]
    return Lookup("script", job_id, " ".join(argv), True, "%d #SBATCH lines" % len(sbatch), text, ms)


async def _job_queue(job_id: str, state: str, timeout: float) -> Optional[Lookup]:
    """For a pending job, Slurm's expected start and reason; for a running one, time left."""
    if state.startswith("PENDING"):
        argv = ["squeue", "-h", "-j", job_id, "--start", "-o", "%i|%S|%r"]
        rc, out, ms = await _run(argv, timeout)
        parts = out.split("|") if rc == 0 and out.count("|") >= 2 else None
        if not parts:
            return None
        start, reason = parts[1].strip(), parts[2].strip()
        summary = ("no start time estimated yet" if start in ("N/A", "", "Unknown") else "expected start " + start) \
            + " (reason: %s)" % reason
        return Lookup("queue", job_id, "squeue -j %s --start" % job_id, True, summary, summary, ms)
    if state.startswith("RUNNING"):
        argv = ["squeue", "-h", "-j", job_id, "-O", "TimeLeft"]
        rc, out, ms = await _run(argv, timeout)
        if rc != 0 or not out.strip():
            return None
        summary = "time left " + out.strip()
        return Lookup("queue", job_id, "squeue -j %s -O TimeLeft" % job_id, True, summary, summary, ms)
    return None


async def _user_queue(user: str, timeout: float) -> Lookup:
    """`squeue -u <user>`: their jobs now, with expected start (pending) and time left (running)."""
    argv = ["squeue", "-h", "-u", user, "-o", "%i|%P|%j|%T|%S|%L|%r"]
    rc, out, ms = await _run(argv, timeout)
    shown = "squeue -u %s --start" % user
    rows = [l.split("|") for l in out.splitlines() if l.count("|") >= 6]
    if rc != 0:
        return Lookup("queue", user, shown, False, out or "squeue failed", "", ms)
    if not rows:
        return Lookup("queue", user, shown, True, "no jobs in the queue", "no jobs in the queue", ms)
    counts: Dict[str, int] = {}
    for r in rows:
        counts[r[3]] = counts.get(r[3], 0) + 1
    summary = "%d jobs: %s" % (len(rows), ", ".join("%d %s" % (n, st) for st, n in sorted(counts.items())))
    lines = [summary, "JOBID|PARTITION|NAME|STATE|START_TIME|TIME_LEFT|REASON"] + ["|".join(r) for r in rows[:12]]
    if len(rows) > 12:
        lines.append("... and %d more" % (len(rows) - 12))
    return Lookup("queue", user, shown, True, summary, "\n".join(lines), ms)


async def _fairshare(user: str, timeout: float) -> Lookup:
    """`sshare`: the user's FairShare, and how their PI group's usage is spread.
    FairShare is per group, so one member's heavy use slows everyone in it.
    Other members are counted, never named."""
    argv = ["sshare", "-a", "-P", "-u", user]
    rc, out, ms = await _run(argv, timeout)
    shown = "sshare -a -u %s" % user
    rows = [l.split("|") for l in out.splitlines() if "|" in l]
    if rc != 0 or len(rows) < 2:
        return Lookup("fairshare", user, shown, False, out or "no FairShare record", "", ms)
    head = [h.strip() for h in rows[0]]
    mine = [dict(zip(head, r)) for r in rows[1:] if len(r) == len(head) and r[head.index("User")].strip() == user] \
        if "User" in head else []
    if not mine:
        return Lookup("fairshare", user, shown, False, "no FairShare record for %s" % user, "", ms)
    me = mine[0]
    account = me.get("Account", "").strip()
    fs = me.get("FairShare", "").strip()
    try:
        low = float(fs) < 0.5
    except ValueError:
        low = False
    summary = "FairShare %s%s, group %s" % (fs, " (below 0.5: expect long waits)" if low else "", account or "?")
    lines = [summary]
    if account and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", account):
        rc2, out2, ms2 = await _run(["sshare", "-a", "-P", "-A", account], timeout)
        ms += ms2
        grows = [dict(zip(head, r.split("|"))) for r in out2.splitlines()[1:] if r.count("|") == len(head) - 1]
        members = [g for g in grows if g.get("User", "").strip()]
        try:
            usage = sorted((float(g.get("EffectvUsage") or 0) for g in members), reverse=True)
        except ValueError:
            usage = []
        if members:
            lines.append("group %s: %d users" % (account, len(members)))
        if usage and usage[0] > 0:
            top = usage[0] / (sum(usage) or 1)
            lines.append("the heaviest user accounts for %.0f%% of the group's usage%s" % (
                100 * top, "" if float(me.get("EffectvUsage") or 0) < usage[0] else " (this user)"))
    return Lookup("fairshare", user, shown, True, summary, "\n".join(lines), ms)


async def _history(user: str, timeout: float) -> Lookup:
    """`sacct -u <user>` over the last 7 days: counts by state and the jobs that did not complete."""
    argv = ["sacct", "-u", user, "-S", "now-7days", "-X", "-P", "-o",
            "JobID,JobName%30,Partition,State,ExitCode,NodeList,End"]
    rc, out, ms = await _run(argv, timeout)
    shown = "sacct -u %s -S now-7days -X" % user
    lines = [l for l in out.splitlines() if l.count("|") >= 6]
    if rc != 0 or not lines:
        return Lookup("history", user, shown, False, out or "sacct failed", "", ms)
    rows = [l.split("|") for l in lines[1:]]
    if not rows:
        return Lookup("history", user, shown, True, "no jobs in the last 7 days", "no jobs in the last 7 days", ms)
    counts: Dict[str, int] = {}
    for r in rows:
        st = r[3].split()[0] if r[3] else "?"
        counts[st] = counts.get(st, 0) + 1
    summary = "last 7 days, %d jobs: %s" % (len(rows), ", ".join("%d %s" % (n, st) for st, n in sorted(counts.items())))
    bad = [r for r in rows if not r[3].startswith(("COMPLETED", "RUNNING", "PENDING"))]
    out_lines = [summary] + (["not completed (newest last):", lines[0]] + ["|".join(r) for r in bad[-10:]] if bad else [])
    return Lookup("history", user, shown, True, summary, "\n".join(out_lines), ms)


async def _project_owner(name: str, timeout: float) -> Lookup:
    """`stat /projects/<name>`: who owns the project directory (the PI, usually)."""
    path = "/projects/" + name
    argv = ["stat", "-c", "%U|%G", path]
    rc, out, ms = await _run(argv, timeout)
    if rc != 0 or "|" not in out:
        return Lookup("project", name, " ".join(argv), False, "no directory %s" % path, "", ms)
    owner, group = out.strip().split("|", 1)
    if owner in ("nobody", "UNKNOWN") or owner.isdigit():
        # NFS without id mapping on this node: the name is not the real owner.
        return Lookup("project", name, " ".join(argv), False, "owner of %s not visible from this node" % path, "", ms)
    summary = "%s is owned by %s, group %s" % (path, owner, group)
    return Lookup("project", name, " ".join(argv), True, summary, summary, ms)


_LDCONFIG = os.environ.get("RC_LDCONFIG", "/sbin/ldconfig")


async def _libraries(names: List[str], timeout: float) -> List[Lookup]:
    """`ldconfig -p`, once, filtered here for the libraries the ticket names.
    Only says what this node has; compute nodes share its image."""
    argv = [_LDCONFIG, "-p"]
    rc, out, ms = await _run(argv, timeout)
    if rc != 0:
        return []
    found = []
    for lib in names:
        stem = lib.split(".so")[0] + ".so"
        hits = [l.strip() for l in out.splitlines() if l.strip().startswith(stem)]
        exact = [h for h in hits if h.startswith(lib + " ") or h.startswith(lib + "\t")]
        if exact:
            summary = "%s is in the system library cache" % lib
        elif hits:
            summary = "%s is not, but other versions are: %s" % (
                lib, ", ".join(sorted({h.split()[0] for h in hits})[:5]))
        else:
            summary = "%s is not in the system library cache (it may come from a module)" % lib
        found.append(Lookup("library", lib, "ldconfig -p | grep %s" % stem, True, summary,
                            "\n".join([summary] + hits[:6]), ms))
    return found


# What the draft needs from `scontrol show node`; the rest (sockets, kernel,
# energy counters) is noise in a prompt.
_NODE_FIELDS = ("NodeName", "State", "Partitions", "Gres", "CPUAlloc", "CPUTot", "RealMemory",
                "AllocMem", "Reason", "BootTime", "LastBusyTime")


async def _node(name: str, timeout: float) -> Lookup:
    """`scontrol show node <name>`: state, partitions and reason, trimmed to _NODE_FIELDS."""
    argv = ["scontrol", "show", "node", name]
    rc, out, ms = await _run(argv, timeout)
    if rc != 0 or "NodeName=" not in out:
        return Lookup("node", name, " ".join(argv), False, "no node named %s" % name, "", ms)
    fields = {}
    for m in re.finditer(r"(\w+)=(.*?)(?=\s+\w+=|\s*$)", out, re.M):
        if m.group(1) in _NODE_FIELDS and m.group(1) not in fields:
            fields[m.group(1)] = m.group(2).strip()
    summary = "%s, partitions %s" % (fields.get("State", "?"), fields.get("Partitions", "?"))
    if fields.get("Reason"):
        summary += ", reason: " + fields["Reason"]
    shown = "\n".join("%s=%s" % (k, fields[k]) for k in _NODE_FIELDS if k in fields)
    return Lookup("node", name, " ".join(argv), True, summary, shown, ms)


async def _partition(name: str, timeout: float) -> Lookup:
    """`sinfo -N -p <name>`: node counts by state, plus the nodes that are down or
    draining with Slurm's reason.
    """
    argv = ["sinfo", "-h", "-N", "-p", name, "-o", "%N|%T|%E"]
    rc, out, ms = await _run(argv, timeout)
    shown = "sinfo -N -p %s" % name
    rows = [l.split("|", 2) for l in out.splitlines() if l.count("|") >= 2]
    if rc != 0 or not rows:
        return Lookup("partition", name, shown, False, "no partition named %s" % name, "", ms)
    nodes: Dict[str, "tuple[str, str]"] = {}
    for node, state, reason in rows:          # a node in two partitions is listed twice
        nodes.setdefault(node, (state, reason))
    counts: Dict[str, int] = {}
    for state, _ in nodes.values():
        counts[state] = counts.get(state, 0) + 1
    summary = "%d nodes: %s" % (len(nodes), ", ".join("%d %s" % (n, s) for s, n in sorted(counts.items())))
    # Only the nodes that are not working normally, with Slurm's reason.
    bad = [(n, s, r) for n, (s, r) in sorted(nodes.items())
           if re.search(r"down|drain|fail|maint|not_responding|unknown|inval", s, re.I)]
    lines = [summary] + ["%s %s%s" % (n, s, (" -- " + r) if r and r != "none" else "") for n, s, r in bad[:15]]
    if len(bad) > 15:
        lines.append("... and %d more" % (len(bad) - 15))
    return Lookup("partition", name, shown, True, summary, "\n".join(lines), ms)


async def run(ticket_text: str, timeout: float = 5.0) -> List[Lookup]:
    """Every lookup the ticket calls for, run concurrently. Never raises: a
    command that is missing, fails or times out becomes a Lookup saying so."""
    c = find_candidates(ticket_text)
    if not (c.users or c.jobs or c.groups or c.nodes or c.partitions or c.projects or c.libraries):
        return []
    try:
        # Users first, so group membership is only reported for accounts that
        # exist, not for every token that happened to look like a username.
        users = list(await asyncio.gather(*[_user(u, timeout) for u in c.users])) \
            if c.users and shutil.which("id") else []
        known = [u.target for u in users if u.ok]
        rest = []
        if c.jobs and shutil.which("sacct"):
            rest += [_job(j, timeout) for j in c.jobs]
        if c.groups and shutil.which("getent"):
            rest += [_group(g, known, timeout) for g in c.groups]
        if c.nodes and shutil.which("scontrol"):
            rest += [_node(n, timeout) for n in c.nodes]
        if c.partitions and shutil.which("sinfo"):
            rest += [_partition(p, timeout) for p in c.partitions]
        if c.projects and shutil.which("stat"):
            rest += [_project_owner(p, timeout) for p in c.projects]
        # Per-user activity only when the ticket is about it, for accounts that exist.
        active = known[:MAX_ACTIVITY_USERS]
        if c.waiting and active and shutil.which("squeue"):
            rest += [_user_queue(u, timeout) for u in active]
        if c.waiting and active and shutil.which("sshare"):
            rest += [_fairshare(u, timeout) for u in active]
        if c.failing and active and shutil.which("sacct"):
            rest += [_history(u, timeout) for u in active]
        found = users + list(await asyncio.gather(*rest))
        # Follow-ups that need a job's state: its script, and its place in the queue.
        jobs = [lk for lk in found if lk.kind == "job" and lk.ok]
        more = [_job_script(j.target, timeout) for j in jobs[:MAX_SCRIPTS]]
        if shutil.which("squeue"):
            more += [_job_queue(j.target, j.summary.split(",")[0], timeout) for j in jobs]
        if c.libraries and os.path.exists(_LDCONFIG):
            libs = await _libraries(c.libraries, timeout)
        else:
            libs = []
        found += [lk for lk in await asyncio.gather(*more) if lk is not None] + libs
        # A word that only looked like a partition or project name is not worth
        # a line, nor is a job with no stored script.
        return [lk for lk in found if lk.ok or lk.kind not in ("partition", "project", "script")]
    except Exception as exc:          # never let a lookup break a draft
        logger.warning("cluster lookups failed: %s", exc)
        return []


def prompt_text(lookups: List[Lookup]) -> str:
    """The lookups as the prompt shows them: command, then its result."""
    parts = []
    for lk in lookups:
        body = lk.output if (lk.ok and lk.kind != "group") else lk.summary
        parts.append("$ %s\n%s" % (lk.command, body or lk.summary))
    return "\n\n".join(parts)
