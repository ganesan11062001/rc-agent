"""Claim extraction for the citation-support check (the scoring needs a graph)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval.support import extract_claims  # noqa: E402


def test_each_marker_gets_the_text_since_the_previous_boundary():
    text = ("Your job ran out of memory [S1]. Run seff to see peak usage [S2], "
            "then resubmit.")
    assert extract_claims(text) == [
        ("Your job ran out of memory", ["S1"]),
        ("Run seff to see peak usage", ["S2"]),
    ]


def test_marker_after_the_full_stop_keeps_its_sentence():
    assert extract_claims("Hi.\n\nScratch is purged monthly. [S3]") == [
        ("Scratch is purged monthly.", ["S3"]),
    ]


def test_grouped_refs_and_steps():
    assert extract_claims("Request the long partition [S2, S4]") == [
        ("Request the long partition", ["S2", "S4"]),
    ]


def test_bare_marker_yields_no_claim():
    assert extract_claims("[S1]") == []
