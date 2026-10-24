"""Repeated sections: one retrievable canonical copy, the rest point at it."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.run import _mark_duplicates  # noqa: E402

SITE = "https://rc.northeastern.edu"


def _row(sid, page, h, retrievable=True, order=0):
    return {"section_id": sid, "page_url": page, "content_hash": h,
            "retrievable": retrievable, "order": order}


def test_page_beats_post_and_event_and_unique_rows_are_untouched():
    by_url = {
        SITE + "/research-computing-team": "page",
        SITE + "/2025/06/26/miniconda": "post",
        SITE + "/calendar_event/intro": "calendar_event",
    }
    rows = [
        _row("post-1-005", SITE + "/2025/06/26/miniconda", "bio"),
        _row("event-2-003", SITE + "/calendar_event/intro", "bio", retrievable=False),
        _row("page-3-007", SITE + "/research-computing-team", "bio"),
        _row("page-3-001", SITE + "/research-computing-team", "unique"),
    ]
    stats = _mark_duplicates(rows, by_url)
    by_id = {r["section_id"]: r for r in rows}

    assert by_id["page-3-007"]["retrievable"] and by_id["page-3-007"]["duplicate_of"] is None
    assert by_id["post-1-005"] == {**by_id["post-1-005"], "retrievable": False,
                                   "duplicate_of": "page-3-007"}
    assert by_id["event-2-003"]["duplicate_of"] == "page-3-007"
    assert by_id["page-3-001"]["retrievable"] and by_id["page-3-001"]["duplicate_of"] is None
    assert stats == {"repeated_texts": 1, "copies_hidden_from_retrieval": 1}


def test_shallowest_url_wins_among_pages():
    by_url = {SITE + "/a/b/c": "page", SITE + "/a": "page"}
    rows = [_row("x", SITE + "/a/b/c", "f"), _row("y", SITE + "/a", "f")]
    _mark_duplicates(rows, by_url)
    assert rows[0]["duplicate_of"] == "y"


def test_docs_copy_is_canonical_over_the_website():
    docs = "https://rc-docs.northeastern.edu/en/latest/gpus/gpuoverview.html"
    rows = [_row("page-1-002", SITE + "/compute", "gpu-table"),
            _row("doc-gpus/gpuoverview-001", docs, "gpu-table")]
    _mark_duplicates(rows, {SITE + "/compute": "page", docs: "doc"})
    assert rows[0]["duplicate_of"] == "doc-gpus/gpuoverview-001"
    assert rows[1]["retrievable"] and rows[1]["duplicate_of"] is None
