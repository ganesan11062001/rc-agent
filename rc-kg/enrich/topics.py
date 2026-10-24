"""Topic list and alias matching.

Topics are a closed vocabulary from topics.yaml. Matching is lexical on
purpose: a section MENTIONS a topic only if one of its aliases is literally
there, so every edge can be explained by pointing at the words. The same
matcher runs on ticket text at query time to pick topics to expand through.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Pattern

import yaml

TOPICS_FILE = Path(__file__).resolve().parents[1] / "topics.yaml"


@dataclass
class Topic:
    name: str
    description: str
    aliases: List[str]
    pattern: Pattern


def _alias_pattern(aliases: List[str]) -> Pattern:
    # Longest first so "Slurm arrays" wins over "Slurm" in the alternation.
    # \b does not work next to "/" or ".", so word boundaries are spelled out
    # as "not preceded/followed by a word character".
    parts = sorted({a.strip() for a in aliases if a.strip()}, key=len, reverse=True)
    body = "|".join(re.escape(p) for p in parts)
    return re.compile(r"(?<![\w/.-])(?:%s)(?![\w-])" % body, re.I)


def load_topics(path: Path = TOPICS_FILE) -> List[Topic]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out: List[Topic] = []
    seen = set()
    for t in raw.get("topics", []):
        name = t["name"].strip()
        if name in seen:
            raise ValueError("duplicate topic %r in %s" % (name, path))
        seen.add(name)
        aliases = [str(a) for a in (t.get("aliases") or [])] + [name]
        out.append(Topic(name, (t.get("description") or "").strip(), aliases,
                         _alias_pattern(aliases)))
    return out


def match_topics(text: str, topics: List[Topic]) -> Dict[str, int]:
    """topic name -> number of alias hits in `text`."""
    hits: Dict[str, int] = {}
    for t in topics:
        n = len(t.pattern.findall(text))
        if n:
            hits[t.name] = n
    return hits
