"""Turn a scraped ServiceNow ticket into the context the model should read.

What the extension sends is the page as rendered: the description, then the
activity stream newest first. Most of that stream is not the conversation.
On real tickets (18 of them, 2026-09/10) a typical page carried ~35 email
addresses, nearly all of them the RC team on every "Email sent" notification,
and those notifications outnumbered the actual comments two to one. With a
4,000-character cut on top, the noise crowded the older messages out.

So, before anything else sees the ticket:

- Dropped: "Email sent" notifications (copies of comments already in the
  thread), every To:/Cc:/From: line and address list, "Show email details",
  avatar initials, signatures after a dashed rule, quoted reply chains.
- Compressed: "Field changes" to one line ("Incident state: New -> In
  Progress"); "Email received" to its subject (the body is the description).
- Dropped too: ServiceNow's automatic acknowledgement ("Thank you for reaching
  out ... Office Hours ... consultation"), which every ticket carries.
- Kept, verbatim: every comment and work note, with its date, author, and
  whether the researcher could see it -- in order, oldest first, so the last
  entry is the latest.
- Labelled: each author as "RC staff" or "researcher". The notifications sent
  to the RC team list its members' addresses (`g.murugan@...`), and an author
  "Patel, Gita" is the address `g.patel`. On INC09000008 the model,
  not seeing that "Rivera, Sam" was RC, told the researcher to contact the
  systems team that Sam had already contacted.

fit() then makes the result fit a character budget: the description, the
first entry and as many of the newest entries as fit, with a marker saying how
many were left out in between.

`Prepared.query` is what retrieval searches: the short description, the
description and the researcher's latest message, without greetings and
sign-offs. Searching the whole thread matched the RC team's own words: the
acknowledgement pulled training and office-hours pages into 15 of 19 tickets,
and staff names and signatures matched staff bios and spotlights.

Nothing here calls the model.
"""

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

ACTIVITY_LABEL = "Work notes / activity:"

# For budgets only (the exact count comes from vLLM afterwards); English text
# with commands and IDs runs ~3.5-4 chars per token, so 3 errs on the safe side.
CHARS_PER_TOKEN = 3.0

# "Additional comments (Customer Viewable Field)•10/21/2026 09:23:18 AM"
_HEADER = re.compile(r"^(?P<type>[^•\n]{2,80})•(?P<when>\d{1,2}/\d{1,2}/\d{4} \d{1,2}:\d{2}:\d{2} [AP]M)\s*$")
_INITIALS = re.compile(r"^[A-Z]{1,3}$")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_ADDRESS_HEADER = re.compile(r"^\s*(?:to|cc|bcc|from|reply-to|sent)\s*:\s*(.*)$", re.I)
_ADDRESS_LIST = re.compile(r"^[\s,;<>]*(?:[^,;\s]*@[^,;\s]+[\s,;<>]*)+$")
_QUOTE_START = re.compile(
    r"^\s*(?:On .{4,200} wrote:|-{2,}\s*Original Message\s*-{2,}|_{10,}|From: .+ Sent: .+)\s*$", re.I)
_RULE = re.compile(r"^\s*-{5,}\s*$")
_BLANKS = re.compile(r"\n{3,}")

_NOISE_LINES = {"show email details", "email sent", "email received"}

# ServiceNow's acknowledgement, posted as a comment on every new ticket.
_AUTO_REPLY = re.compile(r"^\s*Thank you for reaching out to Research Computing\. We will respond", re.I)
_SYSTEM_AUTHORS = {"system", "administrator, system"}
# A notification to this many people or more is to the RC team, not the researcher.
_TEAM_MIN_RECIPIENTS = 3
# "RITM Update: reply from: a.b@northeastern.edu"
_REPLY_FROM = re.compile(r"^\s*(?:RITM Update:\s*)?reply from:\s*(\S+@\S+)\s*$", re.I)

# Greetings and sign-offs carry names (staff's, the researcher's), which is all
# retrieval would find in them.
_GREETING = re.compile(r"^\s*(?:hi|hello|hey|dear|good (?:morning|afternoon|evening))\b[^.?!]{0,40}[,!:]?\s*$", re.I)
_PLEASANTRY = re.compile(r"^\s*(?:i\s+)?hope (?:all'?s|all is|you are|you're|this (?:email|message|note) finds)\b.{0,40}$", re.I)
_SIGN_OFF = re.compile(
    r"^\s*(?:best|thanks|thank you|many thanks|thanks so much|thank you so much|regards|best regards|"
    r"kind regards|warm regards|sincerely|cheers|all the best|thanks again|thx|kr)\b[\s\w,!.]{0,30}$", re.I)
_MAIL_FOOTER = re.compile(r"^\s*(?:get outlook for|sent from my)\b", re.I)
_SUBJECT_PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|subject)\s*:\s*)+", re.I)
QUERY_MAX_CHARS = 1500


@dataclass
class Entry:
    """One activity-stream entry: its ServiceNow kind, time, author, body and the
    author's role once identified.
    """
    kind: str                       # as ServiceNow labels it
    when: Optional[datetime]
    author: str
    body: str
    role: str = ""                  # "RC staff" | "researcher" | "" (unknown)

    def render(self) -> str:
        """The entry as the model reads it: "[date] author, role (label):" then the body."""
        when = self.when.strftime("%Y-%m-%d %H:%M") if self.when else "?"
        k = self.kind.lower()
        if k.startswith("additional comments"):
            label = "comment, visible to the researcher"
        elif k.startswith("work notes"):
            label = "internal work note"
        else:
            label = self.kind.lower()
        who = (self.author or "?") + (", " + self.role if self.role else "")
        head = "[%s] %s (%s)" % (when, who, label)
        return head + (":\n" + self.body if self.body else "")


@dataclass
class Prepared:
    """The cleaned ticket and what was done to it, for the trace and the UI."""
    text: str
    raw_chars: int
    stats: Dict[str, int] = field(default_factory=dict)
    query: str = ""                 # what retrieval searches; see the module docstring


def _strip_addresses(lines: List[str], stats: Dict[str, int]) -> List[str]:
    """Drop To:/Cc:/From: lines and bare address lists, counting the addresses
    removed in `stats`.
    """
    out = []
    for line in lines:
        m = _ADDRESS_HEADER.match(line)
        if m and (_EMAIL.search(m.group(1)) or not m.group(1).strip()
                  or line.strip().lower() in ("to:", "cc:", "from:")):
            stats["addresses_removed"] += len(_EMAIL.findall(line))
            continue
        if _ADDRESS_LIST.match(line) and _EMAIL.search(line):
            stats["addresses_removed"] += len(_EMAIL.findall(line))
            continue
        out.append(line)
    return out


def _clean_text(text: str, stats: Dict[str, int]) -> str:
    """Description or a comment body: no address lines, quoted history or
    signature; the words themselves are untouched."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _QUOTE_START.match(line):
            stats["quoted_chains_removed"] += 1
            lines = lines[:i]
            break
    kept = []
    for line in lines:
        if line.lstrip().startswith(">"):
            continue
        if _RULE.match(line):            # a dashed rule: what follows is a signature
            stats["signatures_removed"] += 1
            break
        if line.strip().lower() in _NOISE_LINES:
            continue
        kept.append(line.rstrip())
    kept = _strip_addresses(kept, stats)
    return _BLANKS.sub("\n\n", "\n".join(kept)).strip()


def _field_changes(body: str) -> str:
    """A "Field changes" body (field name / "new was old" line pairs) as one line,
    "Field: old -> new; ...", capped at 300 characters.
    """
    lines = [l.strip() for l in body.splitlines() if l.strip()]
    pairs, i = [], 0
    while i < len(lines):
        name = lines[i]
        value = lines[i + 1] if i + 1 < len(lines) else ""
        new, sep, old = value.rpartition("was")
        pairs.append("%s: %s -> %s" % (name, old, new) if sep and new and old else "%s: %s" % (name, value))
        i += 2
    return "; ".join(pairs)[:300]


def _subject(body: str) -> str:
    """The Subject of an "Email received" entry, or '' if it has none."""
    lines = [l.strip() for l in body.splitlines()]
    for i, l in enumerate(lines):
        if l.lower() == "subject:" and i + 1 < len(lines):
            return lines[i + 1]
        if l.lower().startswith("subject:"):
            return l[8:].strip()
    return ""


def parse_activity(text: str) -> List[Entry]:
    """The activity stream as entries, in page order (newest first)."""
    lines = text.splitlines()
    heads = [i for i, l in enumerate(lines) if _HEADER.match(l.strip())]
    entries = []
    for n, i in enumerate(heads):
        m = _HEADER.match(lines[i].strip())
        author = lines[i - 1].strip() if i >= 1 else ""
        # The next entry starts at its author line, and its initials line if any.
        end = len(lines)
        if n + 1 < len(heads):
            j = heads[n + 1]
            end = j - 1
            if end - 1 > i and _INITIALS.match(lines[end - 1].strip()):
                end -= 1
        body = "\n".join(lines[i + 1:end])
        try:
            when = datetime.strptime(m.group("when"), "%m/%d/%Y %I:%M:%S %p")
        except ValueError:
            when = None
        entries.append(Entry(m.group("type").strip(), when, author, body))
    return entries


# A forwarded request ("Copying rchelp." over the original email) has almost
# no words of its own; the request is the quoted message (INC09000011).
_FORWARD_MIN_WORDS = 4      # "Copying rchelp." has 2; "My job 123456 failed with OOM." has 5


def _forwarded_message(raw_head: str, stats: Dict[str, int]) -> str:
    """The first quoted message in the description, cleaned, if the
    description's own words are too few to be the request; else ""."""
    desc = raw_head.partition("Description:")[2] if "Description:" in raw_head else raw_head
    lines = desc.splitlines()
    start = next((i for i, l in enumerate(lines) if _QUOTE_START.match(l)), None)
    if start is None:
        return ""
    own = _researcher_words(_clean_text("\n".join(lines[:start]), dict(stats)))
    if len(re.findall(r"[A-Za-z]{2,}", own)) >= _FORWARD_MIN_WORDS:
        return ""
    body = []
    for line in lines[start + 1:]:
        if _QUOTE_START.match(line) or _RULE.match(line):
            break                         # the next quote, or a signature
        body.append(re.sub(r"^\s*>\s?", "", line))
    text = _clean_text("\n".join(body), dict(stats))
    if text:
        stats["forwarded_message_kept"] = 1
    return text


def _team_addresses(entries: List[Entry]) -> set:
    """The RC team's addresses: everyone on a notification sent to the team."""
    team = set()
    for e in entries:
        if e.kind.lower() == "email sent":
            found = {a.lower() for a in _EMAIL.findall(e.body)}
            if len(found) >= _TEAM_MIN_RECIPIENTS:
                team |= found
    return team


def _is_team_member(author: str, team_locals: set) -> bool:
    """ "Patel, Gita" is g.murugan; "Lee, Jordan" is jor.lee."""
    last, _, first = author.partition(",")
    first, last = first.strip().lower(), re.sub(r"\s+", "", last.strip().lower())
    if not (first and last):
        return False
    return any(loc.rsplit(".", 1)[-1] == last and loc[0] == first[0]
               for loc in team_locals if "." in loc)


def _role(e: Entry, team_locals: set, team: set, note_writers: set) -> str:
    """ "RC staff", "researcher", or "" when there is no way to tell."""
    author = e.author.strip().lower()
    if author in _SYSTEM_AUTHORS:
        return ""
    if author in note_writers:
        return "RC staff"             # only staff can write work notes
    reply_from = next((m.group(1).lower().rstrip(">") for m in map(_REPLY_FROM.match, e.body.splitlines()[:3])
                       if m), None)
    if team_locals and _is_team_member(e.author, team_locals):
        return "RC staff"
    if reply_from:
        # Emailed into the ticket. Staff mostly comment in ServiceNow itself,
        # so without a team list an emailed reply is taken as the researcher's.
        return "RC staff" if reply_from in team else "researcher"
    if not team_locals or e.kind.lower() == "field changes":
        # Field changes on a new ticket are made in the caller's name, and so
        # are the IT Service Desk's: they say nothing about who is who.
        return ""
    return "researcher"


def _researcher_words(text: str) -> str:
    """A message without its greeting, sign-off, signature, mail footer and
    reply-from line."""
    lines = [l for l in text.splitlines() if not _REPLY_FROM.match(l) and not _MAIL_FOOTER.match(l)]
    while lines and (not lines[0].strip() or _GREETING.match(lines[0]) or _PLEASANTRY.match(lines[0])):
        lines.pop(0)
    content = 0
    for i, line in enumerate(lines):
        if line.strip() == "--" or (line.strip() and content and _SIGN_OFF.match(line)):
            lines = lines[:i]
            break
        if line.strip():
            content += 1
    # A name alone on the last line ("Pat Jones", "Pat").
    while lines and (not lines[-1].strip() or re.fullmatch(r"\s*[A-Z][a-z'-]+(?: [A-Z][a-z'-]+){0,2}\s*",
                                                              lines[-1])):
        lines.pop()
    return _BLANKS.sub("\n\n", "\n".join(lines)).strip()


def _field(head: str, name: str) -> str:
    """One of the head's labelled fields. The description runs to the end of
    the head: its own lines may look like labels ("srun: Required node ...")."""
    m = re.search(r"^%s:[ \t]*" % re.escape(name), head, re.M)
    if not m:
        return ""
    rest = head[m.end():]
    if name != "Description":
        rest = re.split(r"\n\s*\n|^Description:", rest, maxsplit=1, flags=re.M)[0]
    return rest.strip()


def build_query(head: str, latest_from_researcher: str = "") -> str:
    """Short description + description + the researcher's latest message."""
    subject = _SUBJECT_PREFIX.sub("", _field(head, "Short description"))
    description = _field(head, "Description")
    if not subject and not description:
        description = re.sub(r"^Ticket:.*$", "", head, flags=re.M)
    parts = [subject, _researcher_words(description)]
    latest = _researcher_words(latest_from_researcher)
    if latest and latest not in parts[1]:
        parts.append(latest)
    query = "\n\n".join(p for p in parts if p)
    return query[:QUERY_MAX_CHARS].strip()


def prepare(ticket_text: str) -> Prepared:
    """Clean a ticket; see the module docstring. Text without an activity
    stream (pasted by hand) only gets the address, quote and signature pass."""
    stats = {"addresses_removed": 0, "notifications_removed": 0, "field_changes_compressed": 0,
             "signatures_removed": 0, "quoted_chains_removed": 0, "entries_kept": 0,
             "duplicates_removed": 0, "auto_replies_removed": 0}
    head, sep, activity = ticket_text.partition(ACTIVITY_LABEL)
    entries = parse_activity(activity) if sep else parse_activity(ticket_text)
    if not sep and entries:
        # Pasted text that is itself an activity stream: no head to keep apart.
        first = ticket_text.splitlines()
        cut = min(i for i, l in enumerate(first) if _HEADER.match(l.strip()))
        head, activity, sep = "\n".join(first[:max(0, cut - 2)]), ticket_text, ACTIVITY_LABEL
    if not entries:
        forwarded = _forwarded_message(ticket_text, stats)
        text = _clean_text(ticket_text, stats)
        if forwarded:
            text += "\n\n(forwarded message, quoted in the email)\n" + forwarded
        return Prepared(text, len(ticket_text), stats, build_query(text))

    forwarded = _forwarded_message(head, stats)
    head = _clean_text(head, stats)
    if forwarded:
        head += "\n\n(forwarded message, quoted in the email)\n" + forwarded
    team = _team_addresses(entries)
    team_locals = {a.split("@")[0] for a in team}
    note_writers = {e.author.strip().lower() for e in entries if e.kind.lower().startswith("work notes")}
    rendered, seen, latest = [], set(), ""
    stats["staff_identified"] = 0
    for e in sorted(entries, key=lambda e: e.when or datetime.min):
        k = e.kind.lower()
        if k == "email sent":
            stats["notifications_removed"] += 1
            stats["addresses_removed"] += len(_EMAIL.findall(e.body))
            continue
        if k.startswith("additional comments") and e.author.strip().lower() in _SYSTEM_AUTHORS \
                and _AUTO_REPLY.match(e.body):
            stats["auto_replies_removed"] += 1
            continue
        role = _role(e, team_locals, team, note_writers)
        if k == "email received":
            stats["addresses_removed"] += len(_EMAIL.findall(e.body))
            subj = _subject(e.body)
            e = Entry(e.kind, e.when, e.author, ("Subject: " + subj) if subj else "", role)
        elif k == "field changes":
            stats["field_changes_compressed"] += 1
            e = Entry(e.kind, e.when, e.author, _field_changes(e.body), role)
        else:
            e = Entry(e.kind, e.when, e.author, _clean_text(e.body, stats), role)
            if role == "researcher" and k.startswith("additional comments") and e.body:
                latest = e.body
        key = (e.kind, e.author, e.body)
        if e.body and key in seen:
            stats["duplicates_removed"] += 1
            continue
        seen.add(key)
        if role == "RC staff":
            stats["staff_identified"] += 1
        rendered.append(e.render())
    stats["entries_kept"] = len(rendered)
    text = head + "\n\n" + ACTIVITY_LABEL + " (oldest first)\n\n" + "\n\n".join(rendered)
    return Prepared(text.strip(), len(ticket_text), stats, build_query(head, latest))


def fit(prepared: Prepared, max_chars: int) -> Prepared:
    """Within max_chars: keep the head (description), the first activity entry
    and the newest ones; say how many in between were left out."""
    text = prepared.text
    stats = dict(prepared.stats, budget_chars=max_chars, entries_omitted=0)
    if len(text) <= max_chars:
        return Prepared(text, prepared.raw_chars, stats, prepared.query)
    marker = ACTIVITY_LABEL + " (oldest first)\n\n"
    head, sep, rest = text.partition(marker)
    if not sep:
        # No thread to thin out: keep the beginning, which holds the request.
        stats["truncated_chars"] = len(text) - max_chars
        return Prepared(text[:max_chars] + "\n[... truncated to fit the model's context ...]",
                        prepared.raw_chars, stats, prepared.query)
    entries = re.split(r"\n\n(?=\[(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}|\?)\] )", rest)
    head_part = head + sep
    keep_first = entries[:1]
    budget = max_chars - len(head_part) - sum(len(e) + 2 for e in keep_first) - 120
    newest: List[str] = []
    for e in reversed(entries[1:]):
        if budget - (len(e) + 2) < 0:
            break
        newest.insert(0, e)
        budget -= len(e) + 2
    omitted = len(entries) - len(keep_first) - len(newest)
    parts = keep_first + (["[... %d earlier activity entries omitted to fit the model's context ...]" % omitted]
                          if omitted else []) + newest
    out = head_part + "\n\n".join(parts)
    stats["entries_omitted"] = omitted
    if len(out) > max_chars:             # one entry alone is too long
        out = out[:max_chars] + "\n[... truncated to fit the model's context ...]"
        stats["truncated_chars"] = len(text) - max_chars
    return Prepared(out, prepared.raw_chars, stats, prepared.query)
