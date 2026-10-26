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
- Kept, verbatim: every comment and work note, with its date, author, and
  whether the researcher could see it -- in order, oldest first, so the last
  entry is the latest.

fit() then makes the result fit a character budget: the description, the
first entry and as many of the newest entries as fit, with a marker saying how
many were left out in between. Nothing here calls the model.
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


@dataclass
class Entry:
    kind: str                       # as ServiceNow labels it
    when: Optional[datetime]
    author: str
    body: str

    def render(self) -> str:
        when = self.when.strftime("%Y-%m-%d %H:%M") if self.when else "?"
        k = self.kind.lower()
        if k.startswith("additional comments"):
            label = "comment, visible to the researcher"
        elif k.startswith("work notes"):
            label = "internal work note"
        else:
            label = self.kind.lower()
        head = "[%s] %s (%s)" % (when, self.author or "?", label)
        return head + (":\n" + self.body if self.body else "")


@dataclass
class Prepared:
    """The cleaned ticket and what was done to it, for the trace and the UI."""
    text: str
    raw_chars: int
    stats: Dict[str, int] = field(default_factory=dict)


def _strip_addresses(lines: List[str], stats: Dict[str, int]) -> List[str]:
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


def prepare(ticket_text: str) -> Prepared:
    """Clean a ticket; see the module docstring. Text without an activity
    stream (pasted by hand) only gets the address, quote and signature pass."""
    stats = {"addresses_removed": 0, "notifications_removed": 0, "field_changes_compressed": 0,
             "signatures_removed": 0, "quoted_chains_removed": 0, "entries_kept": 0,
             "duplicates_removed": 0}
    head, sep, activity = ticket_text.partition(ACTIVITY_LABEL)
    entries = parse_activity(activity) if sep else parse_activity(ticket_text)
    if not sep and entries:
        # Pasted text that is itself an activity stream: no head to keep apart.
        first = ticket_text.splitlines()
        cut = min(i for i, l in enumerate(first) if _HEADER.match(l.strip()))
        head, activity, sep = "\n".join(first[:max(0, cut - 2)]), ticket_text, ACTIVITY_LABEL
    if not entries:
        text = _clean_text(ticket_text, stats)
        return Prepared(text, len(ticket_text), stats)

    head = _clean_text(head, stats)
    rendered, seen = [], set()
    for e in sorted(entries, key=lambda e: e.when or datetime.min):
        k = e.kind.lower()
        if k == "email sent":
            stats["notifications_removed"] += 1
            stats["addresses_removed"] += len(_EMAIL.findall(e.body))
            continue
        if k == "email received":
            stats["addresses_removed"] += len(_EMAIL.findall(e.body))
            subj = _subject(e.body)
            e = Entry(e.kind, e.when, e.author, ("Subject: " + subj) if subj else "")
        elif k == "field changes":
            stats["field_changes_compressed"] += 1
            e = Entry(e.kind, e.when, e.author, _field_changes(e.body))
        else:
            e = Entry(e.kind, e.when, e.author, _clean_text(e.body, stats))
        key = (e.kind, e.author, e.body)
        if e.body and key in seen:
            stats["duplicates_removed"] += 1
            continue
        seen.add(key)
        rendered.append(e.render())
    stats["entries_kept"] = len(rendered)
    text = head + "\n\n" + ACTIVITY_LABEL + " (oldest first)\n\n" + "\n\n".join(rendered)
    return Prepared(text.strip(), len(ticket_text), stats)


def fit(prepared: Prepared, max_chars: int) -> Prepared:
    """Within max_chars: keep the head (description), the first activity entry
    and the newest ones; say how many in between were left out."""
    text = prepared.text
    stats = dict(prepared.stats, budget_chars=max_chars, entries_omitted=0)
    if len(text) <= max_chars:
        return Prepared(text, prepared.raw_chars, stats)
    marker = ACTIVITY_LABEL + " (oldest first)\n\n"
    head, sep, rest = text.partition(marker)
    if not sep:
        # No thread to thin out: keep the beginning, which holds the request.
        stats["truncated_chars"] = len(text) - max_chars
        return Prepared(text[:max_chars] + "\n[... truncated to fit the model's context ...]",
                        prepared.raw_chars, stats)
    entries = re.split(r"\n\n(?=\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] )", rest)
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
    return Prepared(out, prepared.raw_chars, stats)
