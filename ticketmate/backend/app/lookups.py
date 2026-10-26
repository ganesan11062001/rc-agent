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
  `scontrol show node` and `sinfo`, each with a fixed argument list. Nothing
  that changes, updates or deletes anything is reachable from this module.
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
MAX_OUTPUT_CHARS = 2000

# What an argument must look like, in full, before it is passed to a command.
# Explorer usernames are `<last>.<initial>` or `<initial>.<last>`.
USERNAME = re.compile(r"[a-z][a-z0-9-]{0,30}\.[a-z][a-z0-9-]{0,30}")
JOB_ID = re.compile(r"[1-9][0-9]{4,9}")
GROUP = re.compile(r"[a-z][a-z0-9_-]{2,31}")
# Explorer's compute nodes: c0602, d4043.
NODE = re.compile(r"[a-z]{1,2}[0-9]{4}")
PARTITION = re.compile(r"[a-z][a-z0-9_-]{1,31}")

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


_SACCT_FIELDS = "JobID,JobName%30,User,Account,Partition,State,ExitCode,Elapsed,Timelimit,ReqMem,MaxRSS,AllocCPUS,NodeList,Start,End"


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
    if rc != 0 or not out:
        return Lookup("group", name, " ".join(argv), False, "no group named %s" % name, "", ms)
    members = [m for m in out.split(":")[-1].split(",") if m]
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
    if not (c.users or c.jobs or c.groups or c.nodes or c.partitions):
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
        found = users + list(await asyncio.gather(*rest))
        # A word that only looked like a partition name is not worth a line.
        return [lk for lk in found if lk.ok or lk.kind != "partition"]
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
