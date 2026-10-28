"""The agents' prompts, as versioned files (app/agents/prompts/*.md).

Each prompt is one file, so a change is a reviewable diff, and each has a
version -- the first 8 hex digits of its SHA-256 -- that every draft records
in its trace with the prompts it used. `render(name, **values)` fills
`{placeholders}`; a missing value raises, so a renamed placeholder cannot
silently ship an unfilled prompt.
"""

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Formatter
from typing import Dict

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"


@dataclass(frozen=True)
class Prompt:
    name: str
    text: str
    version: str

    def render(self, **values: str) -> str:
        fields = {f for _, f, _, _ in Formatter().parse(self.text) if f}
        missing = fields - set(values)
        if missing:
            raise KeyError("prompt %s needs %s" % (self.name, ", ".join(sorted(missing))))
        return self.text.format(**values)


@lru_cache
def load_all(directory: str = str(PROMPT_DIR)) -> Dict[str, Prompt]:
    out = {}
    for p in sorted(Path(directory).glob("*.md")):
        text = p.read_text(encoding="utf-8").strip() + "\n"
        out[p.stem] = Prompt(p.stem, text, hashlib.sha256(text.encode()).hexdigest()[:8])
    return out


def get(name: str) -> Prompt:
    try:
        return load_all()[name]
    except KeyError:
        raise KeyError("no prompt %r in %s" % (name, PROMPT_DIR)) from None


def versions() -> Dict[str, str]:
    """name -> version, for the trace."""
    return {n: p.version for n, p in load_all().items()}
