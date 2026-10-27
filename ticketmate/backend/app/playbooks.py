"""RC workflow playbooks: the team's runbooks, put in front of the model when a
ticket is one of the requests they cover.

Some requests are not answered from the website at all; RC has a procedure
for them (AICR project membership, multi-GPU or long-partition access, adding
someone to a project group, the Gaussian group). Each procedure is a Markdown
file in app/playbooks/: a header saying which tickets it is for, then the
runbook itself -- its stages, how to tell from the thread which stage the
ticket is at, the commands for each stage and the reply for it.

When a ticket matches, the runbook goes into the prompt, and the draft
follows it: "suggested_steps" are the next stage's commands with the ticket's
values filled in, "draft_response" is that stage's reply. Nothing here runs a
command. Several of these change things (a reservation, a group, a push to
GitHub); the RC engineer runs them.

File format -- a header between `---` lines, then the runbook:

    ---
    name: long-partition
    title: Long partition access
    match: (?i)\\blong partition\\b          every `match` line must match
    match: (?i)\\b(access|request|add)\\b
    unless: (?i)\\bdrain(ed)?\\b              no `unless` line may match
    field.username: \\b([a-z]+\\.[a-z]+)\\b   values to pull out of the ticket
    require.username|email: their Explorer username (or email)
    ---
    ...the runbook...

`match`/`unless` are tested against what the researcher wrote (subject,
description, latest message: ticket_context's query), so RC's own replies
cannot trigger a playbook -- or, with `on: instructions`, against what the
RC engineer typed for this draft (the follow-up and closing skeletons). `field.*` patterns run on the whole cleaned ticket;
group 1 (or the whole match) is the value. `require.<field>[|<field>...]`
names what RC needs before it can act, satisfied by any one of the fields
listed; when none was found, the text after the colon is what the reply asks
the researcher for (the model is told to check first whether the ticket gives
it some other way, e.g. a project's name instead of its number).
"""

import logging
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger(__name__)

PLAYBOOK_DIR = Path(__file__).resolve().parent / "playbooks"
MAX_PLAYBOOKS = 2          # per draft; each runbook is ~1k tokens
MAX_FIELD_VALUES = 6


@dataclass
class Playbook:
    name: str
    title: str
    body: str
    match: List["re.Pattern[str]"] = field(default_factory=list)
    unless: List["re.Pattern[str]"] = field(default_factory=list)
    fields: Dict[str, "re.Pattern[str]"] = field(default_factory=dict)
    # (fields any one of which satisfies it, what to ask for when none was found)
    requires: List["tuple[List[str], str]"] = field(default_factory=list)
    on: str = "researcher"          # "researcher" | "instructions": which text `match` reads
    path: str = ""

    def missing(self, values: Dict[str, List[str]]) -> List[str]:
        """What to ask for: each requirement none of whose fields has a value."""
        return [ask for keys, ask in self.requires if not any(values.get(k) for k in keys)]

    def applies(self, researcher_text: str) -> bool:
        return (bool(self.match) and all(p.search(researcher_text) for p in self.match)
                and not any(p.search(researcher_text) for p in self.unless))

    def extract(self, ticket_text: str) -> Dict[str, List[str]]:
        found: Dict[str, List[str]] = {}
        for key, pattern in self.fields.items():
            values: List[str] = []
            for m in pattern.finditer(ticket_text):
                v = (m.group(1) if m.groups() else m.group(0)).strip()
                if v and v not in values:
                    values.append(v)
                if len(values) >= MAX_FIELD_VALUES:
                    break
            if values:
                found[key] = values
        return found


def parse(text: str, path: str = "") -> Playbook:
    if not text.startswith("---\n"):
        raise ValueError("%s: no header (a file starts with a '---' line)" % path)
    header, sep, body = text[4:].partition("\n---\n")
    if not sep:
        raise ValueError("%s: header is not closed with a '---' line" % path)
    pb = Playbook(name="", title="", body=body.strip(), path=path)
    for n, line in enumerate(header.splitlines(), 2):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, colon, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not colon or not value:
            raise ValueError("%s:%d: expected 'key: value'" % (path, n))
        try:
            if key == "name":
                pb.name = value
            elif key == "title":
                pb.title = value
            elif key == "on":
                if value not in ("researcher", "instructions"):
                    raise ValueError("%s:%d: 'on' is 'researcher' or 'instructions'" % (path, n))
                pb.on = value
            elif key == "match":
                pb.match.append(re.compile(value))
            elif key == "unless":
                pb.unless.append(re.compile(value))
            elif key.startswith("field."):
                pb.fields[key[6:]] = re.compile(value)
            elif key.startswith("require."):
                pb.requires.append(([k.strip() for k in key[8:].split("|") if k.strip()], value))
            else:
                raise ValueError("%s:%d: unknown key %r" % (path, n, key))
        except re.error as exc:
            raise ValueError("%s:%d: bad pattern: %s" % (path, n, exc)) from None
    if not (pb.name and pb.title and pb.match):
        raise ValueError("%s: name, title and at least one match line are required" % path)
    for keys, _ in pb.requires:
        unknown = [k for k in keys if k not in pb.fields]
        if not keys or unknown:
            raise ValueError("%s: require.%s names no field.%s line" % (path, "|".join(keys), "|".join(unknown)))
    return pb


@lru_cache
def load(directory: str = str(PLAYBOOK_DIR)) -> List[Playbook]:
    """Every playbook, sorted by file name. A file that does not parse is
    skipped with an error in the log, so one bad edit cannot stop drafting."""
    out: List[Playbook] = []
    for p in sorted(Path(directory).glob("*.md")):
        try:
            out.append(parse(p.read_text(encoding="utf-8"), p.name))
        except ValueError as exc:
            logger.error("playbook skipped: %s", exc)
    return out


@dataclass
class Match:
    playbook: Playbook
    values: Dict[str, List[str]]

    @property
    def missing(self) -> List[str]:
        return self.playbook.missing(self.values)

    @property
    def urls(self) -> List[str]:
        """Links the runbook itself gives, so the citation check lets them through."""
        found = re.findall(r"https?://[^\s)`<>\"']+", self.playbook.body)
        return list(dict.fromkeys(u.rstrip(".,;:") for u in found))

    def to_dict(self) -> Dict[str, object]:
        return {"name": self.playbook.name, "title": self.playbook.title, "values": self.values,
                "missing": self.missing}


def find(researcher_text: str, ticket_text: str, playbooks: List[Playbook] = None,
         instructions: str = "") -> List[Match]:
    """The playbooks this ticket calls for, with the values found for each:
    request playbooks first (they say what the ticket is about), then those
    the engineer's instructions call for."""
    books = load() if playbooks is None else playbooks
    hits = [pb for pb in books if pb.on == "researcher" and pb.applies(researcher_text)][:MAX_PLAYBOOKS]
    text = (instructions or "").strip()
    if text:
        hits += [pb for pb in books if pb.on == "instructions" and pb.applies(text)][:1]
    return [Match(pb, pb.extract(ticket_text)) for pb in hits]


def prompt_text(matches: List[Match]) -> str:
    """The runbooks as the prompt shows them, each with the values found."""
    parts = []
    for m in matches:
        found = "\n".join("- %s: %s" % (k, ", ".join(v)) for k, v in m.values.items()) or "- (none)"
        block = "### %s\n\nValues found in the ticket (check them; they are pattern matches):\n%s\n" % (
            m.playbook.title, found)
        if m.missing:
            block += ("\nMISSING -- not found in the ticket, and needed before RC can act:\n"
                      + "\n".join("- " + ask for ask in m.missing) + "\n")
        parts.append(block + "\n" + m.playbook.body)
    return "\n\n".join(parts)


def missing_text(matches: List[Match]) -> List[str]:
    """Everything the matched playbooks still need, in order, without repeats."""
    out: List[str] = []
    for m in matches:
        out += [a for a in m.missing if a not in out]
    return out
