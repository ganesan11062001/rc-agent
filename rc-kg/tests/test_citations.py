"""Rule 2: links in an answer come from retrieval, never from the model."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval.citations import render  # noqa: E402

SITE = "https://rc.northeastern.edu"
OOM = SITE + "/2025/11/04/debugging/#:~:text=1.%20Job%20Went%20Out%20of%20Memory"
PART = SITE + "/partitions/#:~:text=General%20Access"
REFS = {"S1": OOM, "S2": PART}
ALLOWED = [OOM, SITE + "/2025/11/04/debugging/", PART, SITE + "/partitions/",
           "https://rc-docs.northeastern.edu/en/latest/runningjobs/index.html"]


def test_markers_become_exact_links():
    out = render("Request more memory [S1].", REFS, ALLOWED)
    assert out.text == "Request more memory [S1](%s)." % OOM
    assert out.cited == ["S1"]


def test_grouped_markers():
    out = render("See [S1, S2].", REFS, ALLOWED)
    assert "[S1](%s) [S2](%s)" % (OOM, PART) in out.text
    assert out.cited == ["S1", "S2"]


def test_plain_style_for_work_notes():
    out = render("See [S2]", REFS, ALLOWED, style="plain")
    assert out.text == "See [S2] <%s>" % PART


def test_invented_rc_url_is_removed():
    out = render("Read %s/gpu-guide/ now." % SITE, REFS, ALLOWED)
    assert "gpu-guide" not in out.text
    assert out.removed_urls == [SITE + "/gpu-guide/"]


def test_invented_fragment_on_a_retrieved_page_is_cut_to_the_page():
    out = render("See %s/partitions/#made-up-id." % SITE, REFS, ALLOWED)
    assert out.text == "See %s/partitions/." % SITE
    assert out.removed_urls == []


def test_retrieved_external_doc_is_kept_and_trailing_punctuation_survives():
    url = "https://rc-docs.northeastern.edu/en/latest/runningjobs/index.html"
    out = render("Docs: %s." % url, REFS, ALLOWED)
    assert out.text == "Docs: %s." % url


def test_unknown_ref_is_reported_not_linked():
    out = render("As in [S9].", REFS, ALLOWED)
    assert out.unknown_refs == ["S9"]
    assert "S9" not in out.text


def test_unknown_ref_leaves_no_dangling_space():
    out = render("As in [S9].", REFS, ALLOWED)
    assert out.text == "As in."


def test_numbered_style_shares_numbering_across_texts():
    from retrieval.citations import render_many
    out = render_many(["Reply cites [S2] then [S1].", "Step uses [S1, S2]."],
                      REFS, ALLOWED, style="numbered")
    assert out.texts == ["Reply cites [1] then [2].", "Step uses [2] [1]."]
    assert out.references == [{"n": 1, "ref": "S2", "url": PART},
                              {"n": 2, "ref": "S1", "url": OOM}]
