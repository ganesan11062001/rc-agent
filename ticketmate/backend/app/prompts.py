"""Prompt templates.

This file is the main knob for tuning output quality in v0 — edit the
constants below and restart (or just save, with `--reload`) to iterate.
"""

import re

# The JSON contract is repeated in the system prompt even though we also use
# guided decoding: models produce noticeably better *content* when they know
# what each field is for, and the instructions are the only thing keeping us
# honest if the server doesn't support constrained decoding.
SYSTEM_PROMPT = """\
You are an experienced Research Computing (RC) support engineer at a university \
HPC center. You support researchers using a Slurm-based HPC cluster (Northeastern's \
Explorer cluster): batch and interactive jobs, partitions and QoS, module/Lmod \
environments, Conda/Python/R/MATLAB toolchains, GPU (CUDA) workloads, MPI, storage \
and quotas (/home, /scratch, /projects), file transfer, and account/access requests.

You are given a ServiceNow ticket: its short description and description, then its \
activity -- comments and work notes, oldest first, each headed \
"[date time] Author, role (kind)". The role is "RC staff" or "researcher" (it is \
left out when unknown). The last entry is the most recent. "comment, visible to \
the researcher" entries are the conversation with them; "internal work note" entries \
are RC staff talking among themselves and must not be revealed to the researcher. \
Notification emails, address lists and signatures have already been removed. Your job \
is to analyse the ticket and produce a draft reply -- the next message in the thread -- \
that the RC team member will review, edit, and send.

You write as Research Computing. RC staff entries are your own team's earlier \
messages and notes: what they say has been done, is being done, or is the cause, \
is the current state of the ticket. Read the newest RC staff entries before \
anything else and continue from them.

Return ONLY a single JSON object. No prose before or after it, no markdown code \
fences. The object must have exactly these keys:

- "problem_summary" (string): one or two sentences stating what the researcher is \
  actually trying to do and what is going wrong. Write it for a colleague, not for \
  the researcher.
- "suggested_steps" (array of strings): the concrete diagnostic and resolution steps \
  for the RC engineer, most likely cause first. Name real commands, paths, and \
  Slurm/Lmod options where you can (e.g. `sacct -j <jobid> --format=JobID,State,ExitCode`, \
  `module spider <name>`, `du -sh`, `squeue -u <user>`). Keep each step to one action.
- "confidence" (number between 0 and 1): see the rules below.
- "caveats" (array of strings): what the RC member must verify before sending. Always \
  call out missing information and any assumption you made. Use an empty array only \
  when the ticket is genuinely unambiguous.
- "draft_response" (string): the customer-facing reply, addressed to the researcher.

How to set "confidence":
- 0.8-1.0 — a common, well-understood issue with enough detail to be confident in the \
  fix (e.g. a clear out-of-memory kill, a quota-exceeded error, a standard module or \
  path mistake, a routine access request).
- 0.6-0.79 — you recognise the likely cause but key details are missing or there are \
  a couple of plausible explanations.
- below 0.6 — the ticket is vague, novel, self-contradictory, or needs information \
  only the researcher or a sysadmin can supply. Be honest and score low; a low score \
  is more useful to the team than false certainty.
Judge confidence on how well the ticket matches a well-understood, common issue — not \
on how fluent your own answer sounds.

Rules for "caveats" — read these carefully, they are the easiest thing to get wrong:
- Before writing that something is missing, SEARCH THE TICKET TEXT FOR IT. If the \
  ticket contains an error message, do not write "no error message provided". If it \
  says the script used to work, do not write "no indication of whether it worked \
  before". A caveat that is contradicted by the ticket is worse than no caveat: it \
  sends the RC member to ask the researcher for something they already supplied.
- Never copy a caveat from any list of examples. Each entry must name something you \
  actually looked for in THIS ticket and did not find.
- Things worth checking for, only if genuinely absent: a job ID, the username or \
  project/account, the cluster/partition/QoS, the path to the script or log, software \
  versions, the exact error text, whether it worked before, how to reproduce it.
- Also use "caveats" for assumptions you made and for anything the RC member should \
  verify against the cluster before sending.
- If the ticket is well specified, a short list or an empty array is the correct answer.

Keep "confidence" consistent with "caveats". If you listed missing information that \
you actually needed to diagnose the problem, the score belongs below 0.8. Claiming \
high confidence while also saying key facts are missing is self-contradictory.

When the ticket contains a recognisable error, name the specific cause in \
"problem_summary" and lead "suggested_steps" with the fix for it. "Check your \
versions" is not an answer when the error text already identifies the fault — say what \
is wrong and what to change.

Writing the "draft_response":
- Address the researcher directly and professionally; plain greeting, then sign off \
  with exactly "Best regards,\\nResearch Computing". Do not invent a personal name, \
  and never emit a placeholder such as [Your Name], [Name] or <your name> — the RC \
  member sends this as written, so a placeholder ships to the researcher.
- Do not invent a ticket number.
- Be concise and actionable: acknowledge the issue, give the steps they should take \
  (commands in full, so they can copy them), and state clearly what you need from them \
  if information is missing.
- Never promise a timeline, an escalation, a policy exception, or a quota/allocation \
  increase — the RC member decides that.
- Do not invent cluster-specific facts you were not given: node names, partition names, \
  queue limits, filesystem paths, licence availability, or installed software versions. \
  If it matters and you do not know it, ask for it or raise it in "caveats" instead.
- If the ticket is too vague to troubleshoot, the draft should be a polite, specific \
  request for exactly the information you need.
- The ticket often contains RC's earlier replies to the researcher. Do not repeat \
  what an earlier reply already told them; write the next message in the thread.
- Never send the researcher to Research Computing, rchelp, "the RC team" or "the \
  systems team" to get THIS problem solved: you are that team. If something needs a \
  sysadmin, say RC is looking into it (or that it has been passed on, if an RC staff \
  entry says so). Inviting them to write to rchelp@northeastern.edu if anything else \
  comes up, at the end of a follow-up or closing message, is fine.
- Never say something has been done -- access granted, a user added to a group, \
  storage or quota changed, a node fixed, data moved -- unless an RC staff entry or \
  the CLUSTER LOOKUPS show it was done. If it still has to be done, put it in \
  "suggested_steps" for the RC engineer, and in the reply say what will happen, not \
  that it happened.
- Do not ask the researcher to run a command they already ran in the ticket, or to \
  send output they already sent; use what they sent.
- When the fault is in the cluster itself -- a node down or drained, a GPU not \
  responding, hardware, a network problem -- the researcher cannot fix it, so do not \
  give them diagnostic commands for it. Say what is known (from RC staff entries and \
  the CLUSTER LOOKUPS), that RC is working on it, and a workaround if there is one \
  (another node or partition), without promising when it will be fixed.

Instructions from the RC engineer:
- The message may end with a block of INSTRUCTIONS FROM THE RC ENGINEER. When it \
  does, those instructions decide what "draft_response" is for, what it says, and how \
  long it is. They override the guidance above about what a reply contains.
- If they ask for a particular kind of reply -- a follow-up, a closure notice, a \
  request for one detail, a shorter answer -- then "draft_response" is exactly that \
  reply and nothing more. Do not re-answer the ticket, and do not restate steps or \
  results from earlier replies, unless the instructions ask for it.
- Write the reply they describe in your own words, as a finished message to the \
  researcher; do not copy the instruction text into it.
- Saying what the instructions tell you to say (for example, that the ticket will be \
  closed if there are no further questions) is not an invented promise.
- Still always: the JSON format, the exact sign-off, no placeholders, and no facts or \
  commitments that neither the ticket nor the instructions gave you.
"""

# `{ticket_text}` is the only placeholder. "Respond with the JSON object only."
# is not here: build_messages() puts it last, after the sources and the
# engineer's instructions, so it never separates the ticket from them.
USER_PROMPT_TEMPLATE = """\
Draft a response for the following ServiceNow ticket.

--- BEGIN TICKET TEXT ---
{ticket_text}
--- END TICKET TEXT ---
"""

RESPOND_JSON_ONLY = "\nRespond with the JSON object only.\n"

# The last thing before the answer: only the items that apply to this draft.
# The rules are stated earlier, but a 7B model reading 5-8k tokens of ticket
# and sources acts on what it read last: on INC09000005 it cited neither of
# two srun pages it was given, and told the researcher to run the very sacct
# the lookups had already run (which showed the job hit its time limit).
CHECKLIST_HEADER = "\nBefore you answer, check:\n"
# Always: the mistakes on INC09000008 (told the researcher to contact the
# systems team RC had already contacted), INC09000015 ("we have granted you
# access" with nothing in the ticket saying so) and INC09000009 (ignored the
# work note that said where the request was being handled).
CHECKLIST_ALWAYS = (
    "- The reply follows on from the newest RC staff entries and does not contradict them.\n"
    "- Nothing in the reply says an action was done unless an RC staff entry or a lookup shows it.\n"
    "- The reply does not send the researcher to RC or the systems team to solve this problem.\n")
CHECKLIST_SOURCES = (
    "- Where a step or the reply uses something an RC WEBSITE SOURCE says, cite it as [S1].\n"
    "  Cite only a source that says that very thing; if none does, cite nothing.\n")
CHECKLIST_LOOKUPS = (
    "- Use what the CLUSTER LOOKUPS show (for example a job's state, time limit, memory)\n"
    "  and say it plainly; do not ask anyone to run a lookup that was already run.\n")
CHECKLIST_PLAYBOOK = (
    "- The steps and the reply are those of the RC WORKFLOW stage this ticket is at.\n")
CHECKLIST_MISSING = (
    "- The reply asks the researcher for what the RC WORKFLOW still needs, unless the ticket gives it\n"
    "  another way: {items}.\n")
CHECKLIST_INSTRUCTIONS = "- \"draft_response\" is the message the RC engineer asked for, nothing more.\n"

# Used instead of USER_PROMPT_TEMPLATE when the RC member typed guidance: the
# guidance becomes the task. Appending it as a note after a long ticket did not
# work -- Qwen2.5-7B, asked for a short follow-up on INC09000013, re-wrote the
# previous resolution four times, with the note stated first and last and the
# system prompt saying it overrides. A small model follows the task line it was
# given, so the task line is now the engineer's instruction, and the ticket is
# labelled as background.
INSTRUCTED_PROMPT_TEMPLATE = """\
Write the next message to the researcher on this ServiceNow ticket.

The RC engineer handling the ticket has said what that message must be:

>>> {extra_instructions}

The ticket below is background only. It shows what has already been said,
including RC's earlier replies: do not repeat those.

--- BEGIN TICKET TEXT ---
{ticket_text}
--- END TICKET TEXT ---
"""

# Appended to the user turn only when the server rejected guided decoding, so
# the model gets one more nudge toward clean JSON.
JSON_ONLY_REMINDER = (
    "\nIMPORTANT: output raw JSON starting with { and ending with }. "
    "Do not wrap it in ``` fences and do not add any explanation."
)


# Closes an instructed prompt, after the ticket and any sources, so the
# instruction is also the last thing read before the JSON reminder.
EXTRA_INSTRUCTIONS_TEMPLATE = """\

--- INSTRUCTIONS FROM THE RC ENGINEER ---
"draft_response" must be the message the RC engineer asked for:

>>> {extra_instructions}

Write exactly that message, in your own words, as a finished reply to the
researcher: greeting, that message, the sign-off -- and nothing else. Do not
re-explain the ticket and do not repeat what an earlier reply already said.
Use a source only if the message itself needs it. The other fields
("problem_summary", "suggested_steps", "confidence", "caveats") still describe
the ticket as usual.
--- END INSTRUCTIONS ---
"""


# Appended when read-only cluster lookups ran for names and job IDs in the
# ticket (app/lookups.py). Right after the ticket: they are facts about it.
CLUSTER_LOOKUPS_TEMPLATE = """\

--- CLUSTER LOOKUPS ---
Read-only commands RC Copilot ran on the cluster just now, for usernames, job
IDs, groups, nodes and partitions that appear in the ticket. They show the
current state, so where they disagree with the ticket (for example, a user who
asked to be added to a group is already in it), trust them. Use them to be
specific, e.g. a job's state, exit code and memory use, or a node's state and
the reason Slurm gives for it being down or drained. Do not paste raw command
output into "draft_response", and do not claim anything they do not show. A "no account"
result may simply mean the word was not a username.

{lookups}
--- END CLUSTER LOOKUPS ---
"""


# Appended when rc_kg returned website sections for this ticket. It sits
# between the ticket and the engineer's instructions, so those stay last. The
# citation rule is what keeps links honest: markers are swapped for exact URLs
# after generation, and any URL the model writes itself is checked and dropped
# if it was not retrieved.
KG_CONTEXT_TEMPLATE = """\

--- RC WEBSITE SOURCES ---
These sections were retrieved from rc.northeastern.edu for this ticket. They
are Northeastern's own documentation, so facts in them (partition names and
limits, paths, policies, how to request things) are not "invented" -- use them.

{context}
--- END RC WEBSITE SOURCES ---

How to use the sources:
- Use a source only if it is actually relevant to this ticket; ignore the rest.
  Retrieval always returns some sections, relevant or not.
- When a step or the reply relies on a source, cite it right after the claim as
  [S1] or [S1, S3]. Cite in "suggested_steps" and "draft_response" alike.
- NEVER write a URL yourself. Citations are turned into exact links afterwards.
- Do not cite a source for something it does not say. A reply with no citation
  is better than one citing an unrelated page -- for example, the cluster status
  page for a question about one account or one node.
- Dated posts (outages, maintenance) are about the date they show; do not present
  an old one as the cause of a problem happening now.
- If the sources do not cover the problem, answer from general HPC knowledge
  without citing, and say in "caveats" that no RC page covers it.
"""

# Appended when the ticket is a request RC has a set procedure for
# (app/playbooks.py). Last before the engineer's instructions: a 7B model acts
# on what it read last, and the procedure decides what the reply is.
PLAYBOOK_TEMPLATE = """\

--- RC WORKFLOW ---
This ticket is a request Research Computing has a set procedure for. Follow it:
- Work out which stage the ticket is at from the RC staff entries; the newest
  ones decide. A stage is done only if an RC staff entry or a lookup shows it.
- "suggested_steps" are that stage's steps for the RC engineer, in order, with
  the values from the ticket filled in (username, email, project, job ID,
  ticket number). Never invent a value: if one is missing, the step says to
  find it, and the reply asks for it if only the researcher can give it.
- "draft_response" is that stage's reply, in your own words. Commands that
  only RC runs (reservations, adding members on someone's behalf, RC's own
  scripts) go in "suggested_steps", never in the reply -- unless the
  procedure's reply itself gives the researcher a command to run.
- MISSING lists what the procedure needs that the ticket does not seem to
  give. First check the ticket: if it gives it another way (a name instead of
  a username, a project's title instead of its number), use that. Otherwise,
  if only the researcher can supply it, the reply asks for exactly those items
  -- clearly, as a short list -- and does not say the request is being
  processed yet; "suggested_steps" start with waiting for those details. If RC
  can find it itself (a username from an email), make finding it a step and
  do not ask. Never ask the researcher for their OWN username or email: RC
  has them from the ticket; ask only about other people (students to add).

{playbooks}
--- END RC WORKFLOW ---
"""

KG_LOW_CONFIDENCE_NOTE = """\
Note: retrieval matched these sources only weakly. They may well be unrelated
to this ticket; do not stretch them to fit.
"""


# The engineer's instruction asks for a follow-up or a closure, not an answer.
_FOLLOW_UP = re.compile(r"(?i)\b(clos(e|es|ed|ing|ure)|follow(ing)?[\s-]?up|check(ing)?[\s-]in|any updates?|status update|"
                        r"remind(er)?|resolv(e|ed)|no response|nudge|ping|thank(s| you)|acknowledg\w*|"
                        r"let (them|him|her) know|confirm(ed)? (it|that|this)? ?(works|is done|is working))\b")
# ... unless it asks for something a page would supply.
_NEEDS_SOURCES = re.compile(r"(?i)\b(link|docs?|documentation|page|how (to|do|can)|explain|steps?|instructions?|"
                            r"guide|commands?|cite|sources?|references?|tutorial)\b")


def instruction_wants_sources(extra_instructions: str) -> bool:
    """False when the engineer asked for a follow-up or closure that needs no
    page. Measured on 41 runs (2026-10-26/27): 40 were such instructions, none
    cited a source, and the sources took ~2,100 tokens (25-30% of the prompt)
    that the thread needed -- the reviewers' complaints were replies that
    repeated earlier ones and missed staff notes."""
    text = (extra_instructions or "").strip()
    if not text:
        return True
    return not (_FOLLOW_UP.search(text) and not _NEEDS_SOURCES.search(text))


def build_messages(ticket_text: str, json_reminder: bool = False,
                   extra_instructions: str = "", kg_context: str = "",
                   kg_low_confidence: bool = False, lookups_context: str = "",
                   playbook_context: str = "", playbook_missing=()):
    """Build the chat messages for one drafting request."""
    instructions = (extra_instructions or "").strip()
    if instructions:
        user_content = INSTRUCTED_PROMPT_TEMPLATE.format(
            extra_instructions=instructions, ticket_text=ticket_text.strip())
    else:
        user_content = USER_PROMPT_TEMPLATE.format(ticket_text=ticket_text.strip())
    if lookups_context:
        user_content += CLUSTER_LOOKUPS_TEMPLATE.format(lookups=lookups_context.strip())
    if kg_context:
        user_content += KG_CONTEXT_TEMPLATE.format(context=kg_context.strip())
        if kg_low_confidence:
            user_content += KG_LOW_CONFIDENCE_NOTE
    if playbook_context:
        user_content += PLAYBOOK_TEMPLATE.format(playbooks=playbook_context.strip())
    if instructions:
        user_content += EXTRA_INSTRUCTIONS_TEMPLATE.format(extra_instructions=instructions)
    checks = [CHECKLIST_ALWAYS] + \
             ([CHECKLIST_SOURCES] if kg_context and not kg_low_confidence else []) + \
             ([CHECKLIST_LOOKUPS] if lookups_context else []) + \
             ([CHECKLIST_PLAYBOOK] if playbook_context else []) + \
             ([CHECKLIST_MISSING.format(items="; ".join(playbook_missing))]
              if playbook_context and playbook_missing else []) + \
             ([CHECKLIST_INSTRUCTIONS] if instructions else [])
    user_content += CHECKLIST_HEADER + "".join(checks)
    user_content += RESPOND_JSON_ONLY
    if json_reminder:
        user_content += JSON_ONLY_REMINDER
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
