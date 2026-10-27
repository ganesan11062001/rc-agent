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
    """Print ok/FAIL for one condition and remember failures."""
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
check(f.query == lp.query and f.query, "fitting keeps the retrieval query")

print("who is who")
check("Rivera, Sam, RC staff (" in t, "a work-note writer is RC staff on every entry")
check("Lovelace, Ada, researcher (comment" in t, "the caller's comments are the researcher's")

TEAM2 = "s.rivera@northeastern.edu, jor.lee@northeastern.edu, x.other@northeastern.edu"
NODE_TICKET = """Ticket: INC0000077

Short description: Re: d4043 GPU not work

Description:
Hi,

Hope all's well.
The nvidia-smi command is not responding on d4043.
srun: Required node not available (down, drained or reserved)

Thanks,
Grace
--
Grace Hopper | Example Lab | 617.555.0100

Work notes / activity:
GH
Hopper, Grace
Additional comments (Customer Viewable Field)•10/22/2026 09:00:00 AM
RITM Update: reply from: g.hopper@northeastern.edu

Hi, it worked this morning but now my job is pending again.

Best,
Grace
CJ
Rivera, Sam
Additional comments (Customer Viewable Field)•10/21/2026 09:23:18 AM
Hello Grace,

I have messaged the systems team to check on why the node is down.
System
Email sent•10/21/2026 09:23:23 AM
Email sent
Subject:
Incident INC0000077 has had comments added.
From:
Northeastern University
To:
%(team)s
Show email details
SK
Lee, Jordan
Field changes•10/21/2026 09:22:36 AM
Incident state
In ProgresswasNew
AS
Administrator, System
Additional comments (Customer Viewable Field)•10/21/2026 09:00:00 AM
Thank you for reaching out to Research Computing. We will respond to your inquiry as soon as possible.

The RC team also offers Office Hours every Wednesday.
""" % {"team": TEAM2}
n = prepare(NODE_TICKET)
check("Rivera, Sam, RC staff (comment" in n.text, "a team address (s.rivera) makes its owner RC staff")
check("Lee, Jordan, RC staff (" in n.text, "jor.lee is Lee, Jordan")
check("Hopper, Grace, researcher (comment" in n.text, "an emailed reply from outside the team is the researcher's")
check("Office Hours" not in n.text and n.stats["auto_replies_removed"] == 1, "the automatic acknowledgement is gone")
qy = n.query
check(qy.startswith("d4043 GPU not work"), "query starts with the subject, without Re: (%r)" % qy[:40])
check("srun: Required node not available" in qy, "a description line that looks like a label is kept")
check("now my job is pending again" in qy, "the researcher's latest message is in the query")
check("messaged the systems team" not in qy, "staff replies are not in the query")
check(not re.search(r"Hope all|Thanks|Best|Grace|617\.555|reply from|Office Hours", qy),
      "no greeting, sign-off, signature or acknowledgement in the query: %r" % qy)
check(prepare("My job 123456 failed with OOM.\n\nThanks,\nAda").query == "My job 123456 failed with OOM.",
      "pasted text: the query is the text without its sign-off")

print("forwarded requests")
FWD = """Ticket: INC0000099

Short description: Re: Adding RA in my directory

Description:
Copying rchelp.

On Thu, Oct 22, 2026 at 6:27 PM, Pat Doe <pat@example.edu<mailto:pat@example.edu>> wrote:
Hello Research Computing Team,

My project path is /projects/doelab/. Could you please add my RA, lee.k, to this project?

Best,
Pat

__________________________________________________
PAT DOE, PhD | Professor of Things

Work notes / activity:
RS
Rivera, Sam
Work notes•10/22/2026 10:00:00 AM
looking
"""
fw = prepare(FWD)
check("Could you please add my RA, lee.k" in fw.text and fw.stats.get("forwarded_message_kept") == 1,
      "a forwarded email with no words of its own keeps the quoted request")
check("add my RA, lee.k" in fw.query, "and retrieval searches it")
check("Professor of Things" not in fw.text, "the quoted message's signature still goes")
ordinary = prepare(TICKET)
check("forwarded_message_kept" not in ordinary.stats and "> my job died" not in ordinary.text,
      "a reply with its own words still drops the quoted chain")

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
