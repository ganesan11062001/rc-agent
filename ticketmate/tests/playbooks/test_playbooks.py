"""RC workflow playbooks (backend/app/playbooks.py, backend/app/playbooks/*.md).

    backend/.venv/bin/python tests/playbooks/test_playbooks.py     exit 0 = pass

Every playbook file parses; each one fires on the requests it is for (worded
the way real tickets word them) and not on look-alikes (a drained multigpu
node is not a multigpu access request); values are pulled out of the ticket;
the runbook reaches the prompt only when one matched. Synthetic tickets, no
real people.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

# RC's playbooks are internal and live outside the repository (cluster/env.sh sets PLAYBOOK_DIR).
import os as _os  # noqa: E402
_PRIVATE = "/projects/rc/projects/rc-playbooks"
if not _os.environ.get("PLAYBOOK_DIR") and _os.path.isdir(_PRIVATE):
    _os.environ["PLAYBOOK_DIR"] = _PRIVATE
if not _os.environ.get("PLAYBOOK_DIR"):
    print("SKIP: RC's playbooks are not available here (set PLAYBOOK_DIR)")
    raise SystemExit(0)
from app import playbooks, prompts  # noqa: E402

fails = []


def check(cond, what):
    """Print ok/FAIL for one condition and remember failures."""
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        fails.append(what)


def names(text):
    return [m.playbook.name for m in playbooks.find(text, text, books)]


print("loading")
books = playbooks.load.__wrapped__()
expected = {"aicr-access", "course-request", "explorer-access", "follow-up-closure", "gaussian", "long-partition",
            "multigpu-access", "pending-fairshare", "storage-extension"}
check({b.name for b in books} == expected, "every playbook file parses: %s" % sorted(b.name for b in books))
check(all(len(b.body) > 300 for b in books), "every playbook has a runbook body")
for bad, why in (("no header", "no header"), ("---\nname: x\n", "not closed"),
                 ("---\nname: x\ntitle: y\n---\nbody", "no match line"),
                 ("---\nname: x\ntitle: y\nmatch: (\n---\nbody", "bad pattern"),
                 ("---\nname: x\ntitle: y\nmatch: a\nfoo: b\n---\nbody", "unknown key")):
    try:
        playbooks.parse(bad, "t.md")
        check(False, "rejects a file with %s" % why)
    except ValueError:
        check(True, "rejects a file with %s" % why)

print("routing")
CASES = [
    ("Request to add students to AIRC Approved Project. Please add a.student@northeastern.edu to our AICR project.",
     ["aicr-access"]),
    ("We want to add a new PhD student to the p2026_0081_neu group on the AICR cluster.", ["aicr-access"]),
    ("Follow-up on AICR Access Request I submitted last week", ["aicr-access"]),
    ("Multi-GPU access request. Describe your benchmarking calculation in detail: I train with PyTorch DDP "
     "and want to scale to 4 GPUs.", ["multigpu-access"]),
    ("My model is too big for one GPU so I need multigpu access", ["multigpu-access"]),
    ("Requesting access to the long partition; my GROMACS run takes 5 days and cannot be checkpointed",
     ["long-partition"]),
    ("Please add my student doe.j to our group /projects/smithlab", ["explorer-access"]),
    ("Adding RA in my Research Computing directory", ["explorer-access"]),
    ("I would like to request an Explorer account to learn GPU programming", ["explorer-access"]),
    ("Request to be added to Gaussian group", ["gaussian"]),
    ("We need more storage for /projects/smithlab, we are at 35 TB", ["storage-extension"]),
    ("RC HPC Cluster Classroom Use for: Doe, Jane. CHEM5641.21803.202710", ["course-request"]),
    ("My jobs have been pending for 3 days and have not started, why?", ["pending-fairshare"]),
    # Look-alikes: no playbook.
    ("Explorer multigpu: H200 nodes d4054 and d4055 drained since Oct 21", []),
    ("Idling issues on the multi-GPU node: my CryoSPARC job was killed as idle", []),
    ("Node d1004 is down, my job is stuck pending with reason ReqNodeNotAvail", []),
    ("Disk quota exceeded when check-quota still shows space available in /projects/lab", []),
    ("Explorer slowdown: sbatch: error: Batch job submission failed. Queue times seem longer", []),
    ("My Slurm job died with OUT_OF_MEMORY", []),
    ("RStudio session aborted when I open an Rmd file", []),
]
for text, want in CASES:
    got = names(text)
    check(got == want, "%-18s <- %s" % (",".join(want) or "(none)", text[:70]) + ("" if got == want else "  got %s" % got))

print("values")
t = """Ticket: INC0000123

Short description: Request to add students to AIRC Approved Project

Description:
Please add a.student@northeastern.edu and b.student@northeastern.edu to AICR project p2026_0062.
"""
m = playbooks.find(t, t, books)[0]
check(m.values.get("project_id") == ["p2026_0062"] and m.values.get("year") == ["2026"],
      "AICR project id and year: %s" % m.values)
check(m.values.get("email") == ["a.student@northeastern.edu", "b.student@northeastern.edu"],
      "emails in order of appearance")
check(m.values.get("ticket") == ["INC0000123"], "ticket number")
n = playbooks.find("AICR project 27 please add me", "Ticket: INC9\nAICR project 27 please add me", books)[0]
check(n.values.get("project_number") == ["27"], "a bare project number: %s" % n.values)
mg = playbooks.find("multigpu access for doe.j, scaling with DDP", "multigpu access for doe.j, scaling with DDP",
                    books)[0]
check(mg.values.get("username") == ["doe.j"], "username for the reservation: %s" % mg.values)

print("missing information")
for b in books:
    check(all(all(k in b.fields for k in keys) for keys, _ in b.requires), "%s: every require names a field" % b.name)
try:
    playbooks.parse("---\nname: x\ntitle: y\nmatch: a\nrequire.nope: ask\n---\nbody", "t.md")
    check(False, "a require naming no field is rejected")
except ValueError:
    check(True, "a require naming no field is rejected")
bare = "Please add my two new students to our AICR project"
mb = playbooks.find(bare, "Ticket: INC5\n" + bare + "\n[2026-10-27 10:00] Doe, Jo, RC staff (field changes):", books)[0]
check(len(mb.missing) == 3 and any("email" in a for a in mb.missing) and any("project number" in a for a in mb.missing)
      and any("year" in a for a in mb.missing), "AICR with no email, project or year: all three asked for (%s)" % mb.missing)
check(not m.missing, "AICR with emails and p2026_0062: nothing missing (%s)" % m.missing)
yr = playbooks.find("add a.b@northeastern.edu to AICR project 27", "add a.b@northeastern.edu to AICR project 27", books)[0]
check(yr.missing == ["the year of that AICR project (e.g. 2026)"], "project number without a year: the year (%s)" % yr.missing)
yr2 = playbooks.find("add a.b@northeastern.edu to AICR project 27 of 2026",
                     "add a.b@northeastern.edu to AICR project 27 of 2026", books)[0]
check(not yr2.missing, "'project 27 of 2026': nothing missing (%s)" % yr2.missing)
ex = playbooks.find("I'd like an Explorer account", "I'd like an Explorer account", books)[0]
check(any("research group" in a for a in ex.missing), "Explorer access without a group or PI: ask which (%s)" % ex.missing)
ex2 = "I'd like an Explorer account, my advisor is Dr. Smith"
check(not any("research group" in a for a in playbooks.find(ex2, ex2, books)[0].missing), "a named PI is enough")
mp = playbooks.prompt_text([mb])
check("MISSING --" in mp and "email address of each person" in mp, "the prompt lists what is missing")
mm = prompts.build_messages("T", playbook_context=mp, playbook_missing=tuple(playbooks.missing_text([mb])))[1]["content"]
check("The reply asks the researcher for what the RC WORKFLOW still needs" in mm.split("Before you answer, check:")[1]
      and "project number" in mm.split("Before you answer, check:")[1], "the checklist names the missing items")
none = prompts.build_messages("T", playbook_context=playbooks.prompt_text([m]))[1]["content"]
check("still needs" not in none.split("Before you answer, check:")[1], "nothing missing: no such checklist line")

print("triggers read the researcher's words, values the whole ticket")
query = "Please add a.student@northeastern.edu to our AICR project"
ticket = query + "\n\n[2026-10-20 10:00] Doe, Jo, RC staff (internal work note):\nproject is p2026_0081"
m = playbooks.find(query, ticket, books)[0]
check(m.values.get("project_id") == ["p2026_0081"], "a value from an RC note is found: %s" % m.values)
check(playbooks.find("Thanks!", "AICR project p2026_0081 multigpu long partition", books) == [],
      "words only in RC's replies trigger nothing")

print("prompt")
pt = playbooks.prompt_text(playbooks.find(t, t, books))
msg = prompts.build_messages("T", playbook_context=pt)[1]["content"]
check("--- RC WORKFLOW ---" in msg and "aicr-access.bash" in msg and "p2026_0062" in msg,
      "the runbook and the values found are in the prompt")
check("RC WORKFLOW stage" in msg.split("Before you answer, check:")[1], "the checklist names the workflow")
check(msg.index("--- RC WORKFLOW ---") > msg.index("--- END TICKET TEXT ---"), "after the ticket")
plain = prompts.build_messages("T")[1]["content"]
check("RC WORKFLOW" not in plain, "no playbook: no workflow block, no checklist line")
check(len(playbooks.find("AICR multigpu long partition gaussian access add request",
                         "x", books)) <= playbooks.MAX_PLAYBOOKS, "at most %d playbooks per draft" % playbooks.MAX_PLAYBOOKS)

print("follow-up and closing skeletons (chosen by the engineer's instruction)")
def with_instr(query, instr):
    return [m.playbook.name for m in playbooks.find(query, query, books, instructions=instr)]
check(with_instr("My Slurm job died with OUT_OF_MEMORY", "") == [], "no instruction: no skeleton")
check(with_instr("My Slurm job died with OUT_OF_MEMORY", "Write a short follow-up: if no questions we will close")
      == ["follow-up-closure"], "a follow-up instruction picks the skeletons")
check(with_instr("Requesting access to the long partition for my 5-day GROMACS run",
                 "Just following up on the previous message")
      == ["long-partition", "follow-up-closure"], "request playbook first, then the skeleton")
for instr in ("The storage space is handled by the other ticket; please follow up on the ticket for more information",
              "tell that we have added the user. Discovery will be closed soon, so use Explorer"):
    check(with_instr("x", instr) == [], "not a follow-up or closing message: no skeleton -- %s" % instr[:45])
for instr in ("Follow up on the preivous reply and tell if no question we will close hte ticekt",
              "Since we haven't heard back from you in a while, we hope everything is working"):
    check(with_instr("x", instr) == ["follow-up-closure"], "typos and pasted wording still count -- %s" % instr[:45])
check(with_instr("thanks, I will close my laptop", "") == [] and
      with_instr("close the ticket please", "explain how to request a GPU") == [],
      "the researcher's words never pick the skeleton; an unrelated instruction neither")
fu = playbooks.find("q", "q", books, instructions="we will close the ticket")[0]
check("https://rc-docs.northeastern.edu/en/explorer-main/managingprojects/commands.html" in fu.urls,
      "a playbook's own links are allowed through the citation check: %s" % fu.urls)
sx = [b for b in books if b.name == "storage-extension"][0]
check("https://bit.ly/NURC-StorageExtension" in playbooks.Match(sx, {}).urls, "form links too")

print("sources for instructed replies")
from app.prompts import instruction_wants_sources as wants  # noqa: E402
check(wants(""), "no instruction: sources")
for t in ("Write a short follow-up on our previous reply: if they have no further questions, we will go ahead "
          "and close the ticket. Only ask for the details we still need (for example the job ID).",
          "Just following up on the previous message. We haven't heard back from you in a while.",
          "Follow up on the previous reply and tell if there is no question we will close the ticket"):
    check(not wants(t), "follow-up/closure: no sources -- %s" % t[:50])
for t in ("Refer the user access policy from rc website and update the user",
          "Follow up and send them the docs link for checkpointing",
          "explain how to request a GPU"):
    check(wants(t), "asks for something a page supplies: sources -- %s" % t[:50])

print("\n%s: %d failure(s)" % ("PASS" if not fails else "FAIL", len(fails)))
sys.exit(1 if fails else 0)
