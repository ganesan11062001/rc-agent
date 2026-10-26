"""Ticket cleaning and fitting (backend/app/ticket_context.py).

    backend/.venv/bin/python tests/ticket_context/test_ticket_context.py    exit 0 = pass

A synthetic ticket in the exact shape the extension scrapes from ServiceNow
(no real people): the activity stream newest first, notification emails with
the RC team's addresses, field changes, an email received, a signature and a
quoted reply chain.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from app.ticket_context import ACTIVITY_LABEL, fit, prepare  # noqa: E402

TEAM = ", ".join("staff%d@northeastern.edu" % i for i in range(20))

TICKET = """Ticket: INC0000042

Short description: job killed

Description:
Hi, my job 4812345 keeps getting killed. I asked for --mem=4G.

Thanks,
Ada
-----------------------------------------
Ada Lovelace
PhD student, Example Lab
ada@example.edu

Work notes / activity:
System
Email sent•10/22/2026 10:00:05 AM
Email sent
Subject:
Incident INC0000042 has had comments added.
From:
Northeastern University
To:
%(team)s
Show email details
RC
Rivera, Sam
Additional comments (Customer Viewable Field)•10/22/2026 10:00:00 AM
Hi Ada,

The job used 31 GB; please request --mem=32G and resubmit.

On Tue, Oct 21, 2026 at 9:00 AM Ada wrote:
> my job died
> again
RC
Rivera, Sam
Work notes•10/22/2026 09:58:00 AM
sacct shows OUT_OF_MEMORY, MaxRSS 31G. Told her to raise --mem.
AL
Lovelace, Ada
Additional comments (Customer Viewable Field)•10/21/2026 04:00:00 PM
It died again: [lovelace.a@explorer-02 ~]$ sbatch run.sh
Submitted batch job 4812345
AL
Lovelace, Ada
Additional comments (Customer Viewable Field)•10/21/2026 04:00:00 PM
It died again: [lovelace.a@explorer-02 ~]$ sbatch run.sh
Submitted batch job 4812345
RC
Rivera, Sam
Field changes•10/21/2026 02:00:00 PM
Incident state
In ProgresswasNew
Assigned to
Rivera, Sam
System
Email sent•10/21/2026 01:00:00 PM
Email sent
Subject:
An update on INC0000042
From:
Northeastern University
To:
ada@example.edu
Show email details
AL
Lovelace, Ada
Email received•10/21/2026 09:00:00 AM
Email received
Subject:
job killed
From:
ada@example.edu
To:
rchelp@northeastern.edu
Cc:
%(team)s
Show email details
""" % {"team": TEAM}

fails = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


p = prepare(TICKET)
t = p.text
print("cleaning")
check("staff3@northeastern.edu" not in t and not re.search(r"^\s*(To|Cc|From):", t, re.M),
      "CC/To/From address lists are gone")
check(p.stats["addresses_removed"] >= 40, "addresses counted (%d)" % p.stats["addresses_removed"])
check(p.stats["notifications_removed"] == 2 and "has had comments added" not in t, "notification emails dropped")
check("Show email details" not in t and not re.search(r"^(RC|AL)$", t, re.M), "UI chrome and initials gone")
check("Ada Lovelace\nPhD student" not in t and "ada@example.edu" not in t, "signature after the dashed rule gone")
check("> my job died" not in t and "wrote:" not in t, "quoted reply chain gone")
check("[lovelace.a@explorer-02 ~]$ sbatch run.sh" in t, "a shell prompt with @ in a comment is kept")
check("I asked for --mem=4G" in t and "please request --mem=32G" in t and "MaxRSS 31G" in t,
      "every message's words are kept")
check("Incident state: New -> In Progress" in t, "field changes compressed to one line")
check("(email received):\nSubject: job killed" in t, "email received reduced to its subject")
check(t.count("It died again") == 1 and p.stats["duplicates_removed"] == 1, "a duplicated entry appears once")
check("(internal work note)" in t and "(comment, visible to the researcher)" in t, "entries say who could see them")
order = [m.group(1) for m in re.finditer(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\]", t, re.M)]
check(order == sorted(order) and order[-1] == "2026-10-22 10:00", "oldest first, latest last: %s" % order)
check(t.index("Description:") < t.index(ACTIVITY_LABEL), "description before the thread")
check(len(t) < len(TICKET) * 0.6, "much shorter (%d -> %d chars)" % (len(TICKET), len(t)))

print("pasted text without an activity stream")
q = prepare("To: a@x.edu, b@x.edu\nMy job 123456 failed with OOM.\n\nOn Mon, X wrote:\n> old")
check(q.text == "My job 123456 failed with OOM.", "only the address line and the quote go: %r" % q.text)

print("fitting")
long = TICKET.replace("Work notes / activity:\n", "Work notes / activity:\n" + "".join(
    "RC\nRivera, Sam\nWork notes•10/%02d/2026 0%d:00:00 AM\nnote number %d %s\n" % (1 + i // 9, i % 9, i, "x" * 300)
    for i in range(60)))
lp = prepare(long)
f = fit(lp, 3000)
check(len(f.text) <= 3000 + 80, "fits the budget (%d chars)" % len(f.text))
check(f.stats["entries_omitted"] > 0 and "earlier activity entries omitted" in f.text, "says what was left out")
check("I asked for --mem=4G" in f.text, "the description is kept")
check("note number 0 " in f.text, "the first entry is kept")
check("please request --mem=32G" in f.text, "the newest entry is kept")
same = fit(p, 100000)
check(same.text == p.text and same.stats["entries_omitted"] == 0, "nothing changes when it already fits")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
