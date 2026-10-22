"""Anchor resolution.

Context for the expectations here: measured on rc.northeastern.edu, only 26 of
279 headings carry an id, and the live theme adds none. So the text-fragment
path is the common case on this site, not the exception.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.anchors import (  # noqa: E402
    ANCHOR_ID,
    ANCHOR_NONE,
    ANCHOR_TEXT_FRAGMENT,
    build_anchor,
    inline_heading_ids,
    summarize,
    text_fragment,
)

PAGE = "https://rc.northeastern.edu/policies"


def test_inline_id_wins_over_everything():
    a = build_anchor(PAGE, "Scratch Space Policy", 2, {}, inline_id="ScratchPol")
    assert a.kind == ANCHOR_ID
    assert a.url_with_anchor == PAGE + "/#ScratchPol"


def test_live_id_is_used_when_there_is_no_inline_id():
    live = {"scratch space policy": "live-scratch"}
    a = build_anchor(PAGE, "Scratch Space Policy", 2, live)
    assert a.kind == ANCHOR_ID
    assert a.url_with_anchor == PAGE + "/#live-scratch"


def test_heading_match_ignores_case_and_punctuation():
    """The API body and the rendered page differ on smart quotes, &amp;,
    trailing colons and spacing, so matching has to be loose."""
    live = {"general policies": "gp"}
    for variant in ("General Policies", "GENERAL POLICIES:",
                    "General  Policies", "General-Policies"):
        assert build_anchor(PAGE, variant, 2, live).kind == ANCHOR_ID, variant


def test_falls_back_to_a_text_fragment():
    a = build_anchor(PAGE, "User Account Continuity Policy", 2, {})
    assert a.kind == ANCHOR_TEXT_FRAGMENT
    assert a.url_with_anchor.startswith(PAGE + "/#:~:text=")
    assert "User%20Account" in a.url_with_anchor


def test_overview_section_gets_the_bare_page_url():
    """Level 0 is the synthetic preamble: it IS the top of the page, so a
    fragment would scroll past the content it names."""
    a = build_anchor(PAGE, "Overview", 0, {})
    assert a.kind == ANCHOR_NONE
    assert a.url_with_anchor == PAGE + "/"


def test_text_fragment_is_percent_encoded():
    """Every character that would break a URL fragment must be escaped --
    especially '&', which would otherwise look like a second directive to the
    Scroll-to-Text parser."""
    frag = text_fragment("Storage & Quotas / 100%")
    assert frag == "Storage%20%26%20Quotas%20%2F%20100%25"
    for raw in (" ", "&", "/"):
        assert raw not in frag


def test_long_headings_are_truncated():
    frag = text_fragment("x" * 200)
    assert len(frag) <= 60


def test_empty_heading_degrades_to_the_page_url():
    a = build_anchor(PAGE, "", 2, {})
    assert a.kind == ANCHOR_NONE
    assert a.url_with_anchor == PAGE + "/"


def test_trailing_slash_is_not_doubled():
    a = build_anchor(PAGE + "/", "Acknowledgements", 2, {"acknowledgements": "ack"})
    assert a.url_with_anchor == PAGE + "/#ack"


def test_inline_heading_ids_are_extracted_and_keyed_loosely():
    html = '<h2 id="gpud-ppTitle">Partitions</h2><h3>No Id Here</h3>'
    ids = inline_heading_ids(html)
    assert ids == {"partitions": "gpud-ppTitle"}


def test_first_id_wins_for_a_repeated_heading():
    """/training/research-computing-fall-training/ really does have two
    headings with the same text and different ids."""
    html = '<h2 id="Events-Calendar">Events</h2><h2 id="Events-List">Events</h2>'
    assert inline_heading_ids(html) == {"events": "Events-Calendar"}


def test_summarize_counts_each_kind():
    anchors = [
        build_anchor(PAGE, "A", 2, {"a": "x"}),
        build_anchor(PAGE, "B", 2, {}),
        build_anchor(PAGE, "Overview", 0, {}),
    ]
    counts = summarize(anchors)
    assert counts[ANCHOR_ID] == 1
    assert counts[ANCHOR_TEXT_FRAGMENT] == 1
    assert counts[ANCHOR_NONE] == 1


def test_repeated_heading_fragment_gets_the_sections_own_words_as_suffix():
    from ingest.anchors import disambiguate
    u = "https://rc.northeastern.edu/post/#:~:text=Solution%3A"
    assert disambiguate(u, "We can define memory usage for our jobs") == u + ",-We%20can%20define%20memory"
