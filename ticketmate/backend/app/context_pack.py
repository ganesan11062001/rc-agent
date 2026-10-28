"""Context engineering for the writer: fit the knowledge-graph sources to a
budget, by rank.

rc_kg formats every source the same way (title, URL, dates, topics, then up
to 2,500 characters of text), so six sources run to ~7.7k characters
(median on the ground truth, 2026-10-28) whatever their rank -- and the
sixth source gets as much room as the first, while the ticket thread is
what gets trimmed when the context window is tight.

`pack` keeps each block's header lines (so every source can still be cited
and dated), and gives its text a share that falls with rank: the first two
keep up to 2,500 characters, the third and fourth 1,600, the rest 900, all
within `budget` in total. Text is cut at a sentence or line end where one is
near. Retrieval metrics are unaffected (the same sources, in the same
order); what changes is how many tokens the writer spends on them.
"""

import re
from typing import Dict, List, Tuple

SEP = "\n\n---\n\n"
RANK_CAPS = (2500, 2500, 1600, 1600, 900, 900, 900, 900)
_HEADER = re.compile(r"^(\[S\d+\] |URL: |Source: |Published: |Topics: |Linked documentation: |Related RC pages: )")


def _cut(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    head = text[:n]
    stop = max(head.rfind(". "), head.rfind("\n"))
    if stop > n * 0.6:
        head = head[:stop + 1]
    return head.rstrip() + " [...]"


def pack(context: str, budget: int = 7000) -> Tuple[str, Dict[str, int]]:
    """(packed context, stats). An empty or already small context is returned as is."""
    if not context or len(context) <= budget:
        return context, {"chars_in": len(context or ""), "chars_out": len(context or ""), "trimmed": 0}
    blocks = context.split(SEP)
    out: List[str] = []
    trimmed, used = 0, 0
    for i, block in enumerate(blocks):
        lines = block.split("\n")
        header = [l for l in lines if _HEADER.match(l)]
        body = "\n".join(l for l in lines if not _HEADER.match(l))
        cap = RANK_CAPS[i] if i < len(RANK_CAPS) else RANK_CAPS[-1]
        room = max(200, min(cap, budget - used - sum(len(h) + 1 for h in header) - len(SEP)))
        new_body = _cut(body, room)
        trimmed += new_body != body
        piece = "\n".join(header[:2] + [new_body] + header[2:]) if header else new_body
        out.append(piece)
        used += len(piece) + len(SEP)
    packed = SEP.join(out)
    return packed, {"chars_in": len(context), "chars_out": len(packed), "trimmed": trimmed}
