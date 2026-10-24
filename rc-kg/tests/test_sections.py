"""Section splitting.

The regression tests at the bottom are the important ones: both bugs they
cover shipped in a first draft of `sections.py` and were only caught by
comparing total stored characters against the source corpus.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.sections import OVERVIEW_HEADING, split_sections  # noqa: E402


def _headings(html):
    return [(s.heading, s.heading_level) for s in split_sections(html)]


def test_splits_on_h2():
    html = "<h2>First</h2><p>one</p><h2>Second</h2><p>two</p>"
    assert _headings(html) == [("First", 2), ("Second", 2)]


def test_splits_on_h3_too():
    html = "<h2>Big</h2><p>a</p><h3>Small</h3><p>b</p>"
    assert _headings(html) == [("Big", 2), ("Small", 3)]


def test_preamble_becomes_overview():
    html = "<p>intro text</p><h2>Then</h2><p>body</p>"
    out = split_sections(html)
    assert out[0].heading == OVERVIEW_HEADING
    assert out[0].heading_level == 0
    assert "intro text" in out[0].text
    assert out[1].heading == "Then"


def test_no_overview_when_page_starts_with_a_heading():
    out = split_sections("<h2>Start</h2><p>body</p>")
    assert [s.heading for s in out] == ["Start"]


def test_h4_stays_inside_its_parent_section():
    html = "<h2>Parent</h2><p>a</p><h4>Sub</h4><p>b</p>"
    out = split_sections(html)
    assert len(out) == 1
    assert "Sub" in out[0].text and "b" in out[0].text


def test_page_with_no_headings_is_one_overview_section():
    out = split_sections("<p>just prose</p><p>more</p>")
    assert len(out) == 1
    assert out[0].heading == OVERVIEW_HEADING


def test_empty_body_yields_nothing():
    assert split_sections("") == []
    assert split_sections("<div></div>") == []


def test_lists_keep_one_item_per_line():
    out = split_sections("<h2>H</h2><ul><li>alpha</li><li>beta</li></ul>")
    assert "- alpha" in out[0].text
    assert "- beta" in out[0].text


def test_ordered_lists_are_numbered():
    out = split_sections("<h2>H</h2><ol><li>first</li><li>second</li></ol>")
    assert "1. first" in out[0].text
    assert "2. second" in out[0].text


def test_table_rows_keep_their_cells_separated():
    html = ("<h2>H</h2><table><tr><th>Partition</th><th>Limit</th></tr>"
            "<tr><td>gpu</td><td>8h</td></tr></table>")
    text = split_sections(html)[0].text
    assert "Partition | Limit" in text
    assert "gpu | 8h" in text


def test_pre_blocks_survive_on_their_own_lines():
    out = split_sections("<h2>H</h2><pre>sbatch job.sh</pre>")
    assert "sbatch job.sh" in out[0].text


def test_hrefs_are_collected_per_section():
    html = ('<h2>A</h2><p><a href="/one/">x</a></p>'
            '<h2>B</h2><p><a href="https://example.com/">y</a></p>')
    out = split_sections(html)
    assert out[0].hrefs == ["/one/"]
    assert out[1].hrefs == ["https://example.com/"]


def test_order_is_sequential():
    out = split_sections("<h2>A</h2><p>a</p><h2>B</h2><p>b</p><h2>C</h2><p>c</p>")
    assert [s.order for s in out] == [0, 1, 2]


def test_content_hash_ignores_order_and_tracks_text():
    a = split_sections("<h2>H</h2><p>same</p>")[0]
    b = split_sections("<h2>H</h2><p>same</p>")[0]
    c = split_sections("<h2>H</h2><p>different</p>")[0]
    assert a.content_hash == b.content_hash
    assert a.content_hash != c.content_hash


# --- regressions ---------------------------------------------------------

def test_headings_inside_block_theme_wrappers_still_split():
    """Block themes nest headings in wrapper divs. A loop over body.children
    would see one div and collapse the page to a single section."""
    html = (
        '<div class="wp-block-group"><div class="wp-block-group__inner">'
        "<h2>Alpha</h2><p>a</p><h2>Beta</h2><p>b</p>"
        "</div></div>"
    )
    assert _headings(html) == [("Alpha", 2), ("Beta", 2)]


def test_nested_wrappers_do_not_duplicate_text():
    """Regression: recursing into wrappers while also appending the wrapper
    itself stored 317k characters for a 207k corpus."""
    html = (
        '<div><div><h2>One</h2><p>AAAA</p></div>'
        '<div><h2>Two</h2><p>BBBB</p></div></div>'
    )
    out = split_sections(html)
    assert len(out) == 2
    assert out[0].text.count("AAAA") == 1
    assert out[1].text.count("BBBB") == 1
    assert "BBBB" not in out[0].text


def test_script_and_style_are_not_text():
    """Regression: the GPU dashboard page inlines ~117 KB of JavaScript, which
    get_text() happily returned as the biggest section in the corpus."""
    html = (
        "<h2>Users</h2>"
        "<script>var x = 'SHOULD_NOT_APPEAR'; function f(){return 1;}</script>"
        "<style>.a{color:red}</style>"
        "<p>real prose</p>"
    )
    out = split_sections(html)
    assert len(out) == 1
    assert "SHOULD_NOT_APPEAR" not in out[0].text
    assert "color:red" not in out[0].text
    assert out[0].text == "real prose"


def test_heading_only_section_is_dropped():
    """A heading with no body cannot support an answer."""
    out = split_sections("<h2>Empty</h2><h2>Full</h2><p>body</p>")
    assert [s.heading for s in out] == ["Full"]
