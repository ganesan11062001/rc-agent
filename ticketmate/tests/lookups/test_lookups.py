"""Cluster lookups (backend/app/lookups.py): read-only, allowlisted, no shell.

    backend/.venv/bin/python tests/lookups/test_lookups.py     exit 0 = pass

Fake `id`, `getent` and `sacct` on PATH record every argv they are given, so
the test can prove what ran: only those three programs, only with arguments
that fully match their patterns, never through a shell -- whatever the
ticket says.
"""
import asyncio
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "backend"))

work = Path(tempfile.mkdtemp())
fakebin = work / "bin"
fakebin.mkdir()
calls = work / "calls.jsonl"
pwned = work / "pwned"

FAKE = """#!/usr/bin/env python3
import json, sys, time
with open(%(calls)r, "a") as f:
    f.write(json.dumps(sys.argv) + "\\n")
name = sys.argv[0].rsplit("/", 1)[-1]
args = sys.argv[1:]
if name == "id":
    if args == ["doe.l"]:
        print("uid=1234(doe.l) gid=100(users) groups=100(users),5001(gaussian),6002(smith_lab)")
    else:
        print("id: '%%s': no such user" %% args[0], file=sys.stderr); sys.exit(1)
elif name == "getent":
    if args == ["group", "gaussian"]:
        print("gaussian:*:5001:doe.l,smith.j,lee.k")
    else:
        sys.exit(2)
elif name == "sacct" and args[:1] == ["-u"]:
    print("JobID|JobName|Partition|State|ExitCode|NodeList|End")
    print("4900001|train|gpu|COMPLETED|0:0|d1001|2026-10-24T10:00:00")
    print("4900002|train|gpu|OUT_OF_MEMORY|0:125|d1001|2026-10-25T10:00:00")
    print("4900003|prep|short|FAILED|1:0|c0101|2026-10-26T09:00:00")
elif name == "sacct" and args[2:] == ["--batch-script"]:
    if args[1] == "4812345":
        print("Batch Script for 4812345")
        print("#!/bin/bash")
        print("#SBATCH --mem=32G")
        print("#SBATCH -p short")
        print("python train.py")
    else:
        print("Batch script for %%s not available" %% args[1]); sys.exit(1)
elif name == "sacct":
    if args[1] == "4812345":
        print("JobID|JobName|User|Account|Partition|State|ExitCode|Elapsed|Timelimit|ReqMem|MaxRSS|AllocCPUS|NodeList|Start|End")
        print("4812345|train|doe.l|lab|short|OUT_OF_MEMORY|0:125|00:12:01|04:00:00|32G||4|c0123|2026-10-25T10:00:00|2026-10-25T10:12:01")
        print("4812345.batch|batch||lab||OUT_OF_MEMORY|0:125|00:12:01||||4|c0123|2026-10-25T10:00:00|2026-10-25T10:12:01")
        print("4812345.0|python||lab||OUT_OF_MEMORY|0:125|00:12:00|||31.8G|4|c0123|2026-10-25T10:00:01|2026-10-25T10:12:01")
    elif args[1] == "7777777":
        time.sleep(30)
    else:
        print("JobID|JobName|User|Account|Partition|State|ExitCode|Elapsed|Timelimit|ReqMem|MaxRSS|AllocCPUS|NodeList|Start|End")
elif name == "scontrol":
    if args == ["show", "node", "d4043"]:
        print("NodeName=d4043 Arch=x86_64 CoresPerSocket=16")
        print("   CPUAlloc=0 CPUEfctv=32 CPUTot=32 CPULoad=0.01")
        print("   Gres=gpu:a100:4(S:0-1)")
        print("   OS=Linux 5.14.0 #1 SMP")
        print("   RealMemory=512000 AllocMem=0 FreeMem=500000 Sockets=2 Boards=1")
        print("   State=DOWN+DRAIN+NOT_RESPONDING ThreadsPerCore=1 TmpDisk=0 Weight=1")
        print("   Partitions=jlab,gpu")
        print("   BootTime=2026-10-01T08:00:00 SlurmdStartTime=2026-10-01T08:01:00")
        print("   Reason=GPU ECC errors [root@2026-10-20T11:12:13]")
    else:
        print("Node %%s not found" %% args[2]); sys.exit(1)
elif name == "sinfo":
    if args[3] == "ce-mri":
        print("c0601|idle|none")
        print("c0602|down*|Not responding")
        print("c0602|down*|Not responding")
        print("c0603|drained|network maintenance")
        print("c0604|allocated|none")
elif name == "squeue":
    if args[:3] == ["-h", "-u", "wait.q"]:
        print("4950001|gpu|train|PENDING|N/A|1-00:00:00|Priority")
        print("4950002|gpu|train|PENDING|2026-10-28T03:15:34|1-00:00:00|Resources")
        print("4950003|short|prep|RUNNING|2026-10-27T09:00:00|3:10:00|None")
elif name == "sshare":
    head = "Account|User|RawShares|NormShares|RawUsage|EffectvUsage|FairShare"
    if args == ["-a", "-P", "-u", "wait.q"]:
        print(head); print("predlab|wait.q|1|0.125000|70454334|0.172298|0.010984")
    elif args == ["-a", "-P", "-A", "predlab"]:
        print(head)
        print("predlab||1|0.001253|408908662|0.034201|")
        print("predlab|wait.q|1|0.125000|70454334|0.172298|0.010984")
        print("predlab|heavy.u|1|0.125000|332527377|0.813207|0.010847")
        print("predlab|idle.u|1|0.125000|0|0.000000|0.011808")
elif name == "stat":
    print("smithlab|smithlab_grp" if args[-1] == "/projects/smithlab" else "nobody|nobody")
elif name == "ldconfig":
    print("3 libs found in cache `/etc/ld.so.cache'")
    print("\tlibcudnn.so.9 (libc6,x86-64) => /usr/lib64/libcudnn.so.9")
    print("\tlibz.so.1 (libc6,x86-64) => /usr/lib64/libz.so.1")
"""
for prog in ("id", "getent", "sacct", "scontrol", "sinfo", "squeue", "sshare", "stat", "ldconfig"):
    p = fakebin / prog
    p.write_text(FAKE % {"calls": str(calls)})
    p.chmod(p.stat().st_mode | stat.S_IXUSR)

os.environ["PATH"] = str(fakebin) + ":/usr/bin:/bin"
os.environ["RC_LDCONFIG"] = str(fakebin / "ldconfig")
xtra = work / "group.xtra"
xtra.write_text("rcgroup:x:1000:a.one,b.two\n")
os.environ["RC_GROUP_XTRA"] = str(xtra)
from app import lookups, prompts  # noqa: E402

READ_ONLY = {"id", "getent", "sacct", "scontrol", "sinfo", "squeue", "sshare", "stat", "ldconfig"}


def shape_ok(prog, args):
    """Every argv a lookup may run, exactly; anything else is a failure."""
    if prog == "id":
        return len(args) == 1 and lookups.USERNAME.fullmatch(args[0])
    if prog == "getent":
        return len(args) == 2 and args[0] == "group" and lookups.GROUP.fullmatch(args[1])
    if prog == "scontrol":
        return len(args) == 3 and args[:2] == ["show", "node"] and lookups.NODE.fullmatch(args[2])
    if prog == "sinfo":
        return (len(args) == 6 and args[:3] == ["-h", "-N", "-p"] and lookups.PARTITION.fullmatch(args[3])
                and args[4:] == ["-o", "%N|%T|%E"])
    if prog == "sacct":
        if args[:1] == ["-u"]:
            return (len(args) == 8 and lookups.USERNAME.fullmatch(args[1])
                    and args[2:7] == ["-S", "now-7days", "-X", "-P", "-o"])
        if args[2:] == ["--batch-script"]:
            return len(args) == 3 and args[0] == "-j" and lookups.JOB_ID.fullmatch(args[1])
        return (len(args) == 4 and args[0] == "-j" and lookups.JOB_ID.fullmatch(args[1])
                and args[2] == "--parsable2" and args[3].startswith("--format="))
    if prog == "squeue":
        return ((args[:3] == ["-h", "-u", args[2]] and lookups.USERNAME.fullmatch(args[2]) and len(args) == 5
                 and args[3] == "-o")
                or (args[:2] == ["-h", "-j"] and lookups.JOB_ID.fullmatch(args[2])
                    and args[3:] in (["--start", "-o", "%i|%S|%r"], ["-O", "TimeLeft"])))
    if prog == "sshare":
        return (len(args) == 4 and args[:2] == ["-a", "-P"]
                and ((args[2] == "-u" and lookups.USERNAME.fullmatch(args[3]))
                     or (args[2] == "-A" and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", args[3]))))
    if prog == "stat":
        return (len(args) == 3 and args[:2] == ["-c", "%U|%G"] and args[2].startswith("/projects/")
                and lookups.PROJECT.fullmatch(args[2][len("/projects/"):]))
    if prog == "ldconfig":
        return args == ["-p"]
    return False

fails = []


def check(cond, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


def ran():
    """The commands the fake binaries recorded, one dict per call."""
    if not calls.exists():
        return []
    return [json.loads(l) for l in calls.read_text().splitlines()]


TICKET = """Hi, please add doe.l to the Gaussian group.
My job 4812345 died, see slurm-4812345.out. Also check JobID: 5550001.
CC: j.smith@northeastern.edu, g.patel@northeastern.edu, rc-team@northeastern.edu
My script run.sh and train.py are in /home/smith.j/project; see https://rc.northeastern.edu/a.b
Ticket INC09000013.
Nasty: x.y;touch %(p)s  job 6660001$(touch %(p)s) `touch %(p)s` 'quote.d' "q.q" a.b|touch
""" % {"p": pwned}

print("candidates")
c = lookups.find_candidates(TICKET)
check(c.users[0] == "doe.l", "username found: %s" % c.users)
check(not any("@" in u or u in ("j.smith", "g.patel") for u in c.users), "emails are never usernames")
check("smith.j" not in c.users and "run.sh" not in c.users and "train.py" not in c.users,
      "paths and file names are not usernames")
check(len(c.users) <= lookups.MAX_USERS and len(c.jobs) <= lookups.MAX_JOBS, "lookups are capped")
check(c.jobs[:2] == ["4812345", "5550001"], "job ids in order of appearance: %s" % c.jobs)
check("23728961" not in c.jobs, "a ticket number is not a job id")
check(c.groups == ["gaussian"], "group found: %s" % c.groups)
for u in c.users:
    check(lookups.USERNAME.fullmatch(u) is not None, "user arg fully matches its pattern: %r" % u)
for j in c.jobs:
    check(lookups.JOB_ID.fullmatch(j) is not None, "job arg fully matches its pattern: %r" % j)

print("running")
t0 = time.time()
found = asyncio.run(lookups.run(TICKET, timeout=2.0))
check(time.time() - t0 < 10, "finished promptly (%.1fs)" % (time.time() - t0))
argvs = ran()
progs = {a[0].rsplit("/", 1)[-1] for a in argvs}
check(progs <= READ_ONLY, "only the read-only programs ran: %s" % sorted(progs))
for a in argvs:
    check(bool(shape_ok(a[0].rsplit("/", 1)[-1], a[1:])), "argv has the fixed shape: %s" % a[1:])
check(not pwned.exists(), "nothing in the ticket was executed")

by = {(lk.kind, lk.target): lk for lk in found}
u = by.get(("user", "doe.l"))
check(u is not None and u.ok and "gaussian" in u.summary, "id: account and groups (%s)" % (u and u.summary))
j = by.get(("job", "4812345"))
check(j is not None and j.ok and "OUT_OF_MEMORY" in j.summary and "31.8G" in j.summary and "32G" in j.summary,
      "sacct: state, requested and max memory (%s)" % (j and j.summary))
g = by.get(("group", "gaussian"))
check(g is not None and g.ok and "3 members" in g.summary and "includes doe.l" in g.summary,
      "getent: size and membership, not the member list (%s)" % (g and g.summary))
check(g is not None and "smith.j" not in g.output, "other members are not passed on")
check(g is not None and "x.y" not in g.summary, "membership only for accounts that exist (%s)" % (g and g.summary))
miss = [lk for lk in found if lk.kind == "user" and not lk.ok]
check(all("no account" in lk.summary for lk in miss), "unknown users reported as such")

print("nodes and partitions")
NODES = """d4043 GPU not work. squeue shows (ReqNodeNotAvail, UnavailableNodes:d4043).
Many of the nodes on ce-mri and nosuchpart are down. Not nodes: explorer-02, c0602.out, /home/c0603
Nasty: d4044;touch %(p)s  -p x$(touch %(p)s)
""" % {"p": pwned}
nc = lookups.find_candidates(NODES)
check(nc.nodes == ["d4043", "d4044"], "node names found, not hostnames/files/paths: %s" % nc.nodes)
check(nc.partitions[:2] == ["ce-mri", "nosuchpart"], "partitions from 'nodes on X and Y': %s" % nc.partitions)
nf = asyncio.run(lookups.run(NODES, timeout=2.0))
nby = {(lk.kind, lk.target): lk for lk in nf}
nd = nby.get(("node", "d4043"))
check(nd is not None and nd.ok and "DOWN+DRAIN" in nd.summary and "GPU ECC errors" in nd.summary,
      "scontrol: state and Slurm's reason (%s)" % (nd and nd.summary))
check(nd is not None and "OS=" not in nd.output and "Gres=gpu:a100:4" in nd.output,
      "only the node fields a draft needs are passed on")
check(not nby[("node", "d4044")].ok and "no node" in nby[("node", "d4044")].summary, "unknown node reported")
pt = nby.get(("partition", "ce-mri"))
check(pt is not None and pt.ok and pt.summary.startswith("4 nodes") and "c0602 down* -- Not responding" in pt.output
      and "c0603 drained -- network maintenance" in pt.output and "c0601" not in pt.output,
      "sinfo: counts by state, problem nodes with reasons, a node listed twice counted once (%s)"
      % (pt and pt.output.replace("\n", " | ")))
check(("partition", "nosuchpart") not in nby, "a word that is not a partition leaves no line")
check(not pwned.exists(), "nothing in the ticket was executed")
check(all(p[0].rsplit("/", 1)[-1] in READ_ONLY and shape_ok(p[0].rsplit("/", 1)[-1], p[1:]) for p in ran()),
      "still only read-only programs, each with its fixed argv")

print("queue, FairShare, history, scripts, projects, libraries")
j = {(lk.kind, lk.target): lk for lk in found}
sc = j.get(("script", "4812345"))
check(sc is not None and sc.ok and "#SBATCH --mem=32G" in sc.output, "a job's batch script is shown (%s)" % (sc and sc.summary))
check(("script", "5550001") not in j, "a job with no stored script leaves no line")
hi = j.get(("history", "doe.l"))
check(hi is not None and "1 FAILED" in hi.summary and "4900002" in hi.output and "4900001" not in hi.output,
      "'died' in the ticket: recent jobs, only the ones that did not complete listed (%s)" % (hi and hi.summary))
check(not any(lk.kind in ("queue", "fairshare") for lk in found), "no queue or FairShare lookups when nothing is waiting")
check(not any(lk.kind == "library" for lk in found), "no library lookup when none is named")

ids_before = len(ran())
WAIT = """Ticket INC1. My jobs have been pending for days, account wait.q. Why is nothing starting?
Also error while loading shared libraries: libcudnn.so.8 and I use /projects/smithlab and /projects/other_lab
"""
(fakebin / "id").write_text((fakebin / "id").read_text().replace('if args == ["doe.l"]:',
                                                                 'if args in (["doe.l"], ["wait.q"]):'))
wf = asyncio.run(lookups.run(WAIT, timeout=2.0))
w = {(lk.kind, lk.target): lk for lk in wf}
q = w.get(("queue", "wait.q"))
check(q is not None and "2 PENDING" in q.summary and "1 RUNNING" in q.summary and "Priority" in q.output,
      "waiting: the user's queue with start times and reasons (%s)" % (q and q.summary))
fs = w.get(("fairshare", "wait.q"))
check(fs is not None and "0.010984" in fs.summary and "below 0.5" in fs.summary and "3 users" in fs.output
      and "heaviest user accounts for 83%" in fs.output and "heavy.u" not in fs.output,
      "FairShare: score, group size, heaviest share, other members not named (%s)" % (fs and fs.output.replace("\n", " | ")))
check(any(lk.kind == "history" for lk in wf), "an error message in the ticket brings recent failures too")
pr = w.get(("project", "smithlab"))
check(pr is not None and pr.ok and "owned by smithlab" in pr.summary, "project owner (%s)" % (pr and pr.summary))
check(("project", "other_lab") not in w, "an owner that is not visible ('nobody') leaves no line")
lb = w.get(("library", "libcudnn.so.8"))
check(lb is not None and "other versions are: libcudnn.so.9" in lb.summary, "library: other versions named (%s)" % (lb and lb.summary))
new_calls = ran()[ids_before:]
check(all(c_[0].rsplit("/", 1)[-1] in READ_ONLY and shape_ok(c_[0].rsplit("/", 1)[-1], c_[1:]) for c_ in new_calls),
      "every new lookup ran with its fixed, read-only argv")
g2 = asyncio.run(lookups.run("please add me to the rcgroup group", timeout=2.0))
gx = {(lk.kind, lk.target): lk for lk in g2}.get(("group", "rcgroup"))
check(gx is not None and gx.ok and gx.summary.startswith("2 members"), "a group only in group.xtra is found (%s)" % (gx and gx.summary))

print("timeouts and missing commands")
t0 = time.time()
slow = asyncio.run(lookups.run("job 7777777 hangs", timeout=1.0))
check(time.time() - t0 < 5 and slow and not slow[0].ok and "timed out" in slow[0].summary,
      "a hanging command is cut off (%.1fs)" % (time.time() - t0))
(fakebin / "sacct").unlink()
none = asyncio.run(lookups.run("job 4812345", timeout=1.0))
check(none == [], "no sacct on PATH: job lookups skipped, no error")
check(asyncio.run(lookups.run("nothing to look up here", timeout=1.0)) == [], "nothing to look up: nothing runs")

print("prompt")
text = lookups.prompt_text(found)
m = prompts.build_messages("T", lookups_context=text)[1]["content"]
check("--- CLUSTER LOOKUPS ---" in m and "$ id doe.l" in m and "$ sacct -j 4812345" in m,
      "results are in the prompt as commands and output")
check("--- CLUSTER LOOKUPS ---" not in prompts.build_messages("T")[1]["content"], "no lookups: no block")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
