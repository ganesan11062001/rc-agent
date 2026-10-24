"""eval/ragas_llm.py turns TicketMate run logs into RAGAS samples.

Regression: the first source was dropped because its chunk starts with the
prompt's instructions, so Faithfulness would have been judged without the
top-ranked source.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.ragas_llm import samples_from_runs  # noqa: E402

PROMPT = """Draft a response for the following ServiceNow ticket.

--- BEGIN TICKET TEXT ---
job killed OOM
--- END TICKET TEXT ---

Respond with the JSON object only.

--- RC WEBSITE SOURCES ---
These sections were retrieved from rc.northeastern.edu for this ticket. They
are Northeastern's own documentation.

[S1] Debugging > Out of Memory
URL: https://rc.northeastern.edu/x/#a
The job exceeded its memory.

---

[S2] Debugging > Solution:
URL: https://rc.northeastern.edu/x/#b
Use #SBATCH --mem=3G.
--- END RC WEBSITE SOURCES ---
"""


def _write(tmp, name, prompt, draft):
    d = tmp / name
    d.mkdir(parents=True)
    (d / "r.json").write_text(json.dumps({
        "model": "m", "input": {"ticket_text": "job killed OOM"},
        "prompt": [{"role": "system", "content": "sys"}, {"role": "user", "content": prompt}],
        "raw_response": json.dumps(draft)}))


def test_every_source_becomes_a_context_and_markers_are_stripped(tmp_path):
    _write(tmp_path, "RCKG-EVAL-01", PROMPT, {
        "draft_response": "It ran out of memory [S1]. Raise --mem [S2].",
        "suggested_steps": ["Check seff [S1, S2]"]})
    [s] = samples_from_runs(tmp_path, "RCKG-EVAL", 0)
    assert [c.split("\n", 1)[0] for c in s["retrieved_contexts"]] == \
        ["[S1] Debugging > Out of Memory", "[S2] Debugging > Solution:"]
    assert "These sections were retrieved" not in s["retrieved_contexts"][0]
    assert "[S" not in s["response"] and "Raise --mem." in s["response"]
    assert s["user_input"] == "job killed OOM"


def test_ungrounded_drafts_and_other_tickets_are_skipped(tmp_path):
    _write(tmp_path, "RCKG-EVAL-01", "no sources here", {"draft_response": "x"})
    _write(tmp_path, "INC0001", PROMPT, {"draft_response": "x"})
    assert samples_from_runs(tmp_path, "RCKG-EVAL", 0) == []
    assert len(samples_from_runs(tmp_path, "", 0)) == 1
