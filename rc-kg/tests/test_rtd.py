"""rc-docs (Sphinx) ingestion: URL mapping, page cleanup, anchors."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest import anchors as anchors_mod  # noqa: E402
from ingest.rtd import _clean_article, canonical_doc_url  # noqa: E402
from ingest.sections import split_sections  # noqa: E402

D = "https://rc-docs.northeastern.edu/en/latest/"

PAGE = """<html><body><div class="sidebar">nav junk</div>
<article role="main" id="furo-main-content">
<section id="slurm-jobs-array">
<h1>Slurm Jobs Array<a class="headerlink" href="#slurm-jobs-array" title="Link to this heading">¶</a></h1>
<p>Job arrays submit many similar jobs at once. See <a href="../gpus/index.html">GPUs</a>.</p>
<section id="job-array-examples">
<h2>Job Array Examples<a class="headerlink" href="#job-array-examples">¶</a></h2>
<div class="highlight"><pre>#SBATCH --array=1-10</pre></div>
</section>
</section>
</article></body></html>"""


def test_any_version_and_form_maps_to_the_latest_file_url():
    for u in ("https://rc-docs.northeastern.edu/en/explorer-main/gpus/gpuoverview.html",
              "https://rc-docs.northeastern.edu/en/latest/gpus/gpuoverview.html#gpu-table",
              "https://rc-docs.northeastern.edu/en/latest/gpus/gpuoverview"):
        assert canonical_doc_url(u) == D + "gpus/gpuoverview.html"
    assert canonical_doc_url("https://rc-docs.northeastern.edu/en/latest/runningjobs/") == \
        D + "runningjobs/index.html"
    assert canonical_doc_url("https://rc-docs.northeastern.edu/en/latest/") == D + "index.html"


def test_bare_version_and_ambiguous_directory_forms():
    # URL normalization strips trailing slashes, so these arrive slashless.
    assert canonical_doc_url("https://rc-docs.northeastern.edu/en/latest") == D + "index.html"
    known = {D + "software/index.html"}
    assert canonical_doc_url("https://rc-docs.northeastern.edu/en/explorer-main/software", known) == \
        D + "software/index.html"
    assert canonical_doc_url("https://rc-docs.northeastern.edu/en/latest/faq", {D + "faq.html"}) == D + "faq.html"


def test_non_pages_are_not_mapped():
    assert canonical_doc_url(D + "_downloads/abc/Projects_Commands_Cheatsheet.pdf") is None
    assert canonical_doc_url(D + "_images/x.png") is None
    assert canonical_doc_url("https://rc.northeastern.edu/partitions") is None


def test_cleanup_takes_the_title_drops_pilcrows_and_moves_section_ids():
    title, html = _clean_article(PAGE)
    assert title == "Slurm Jobs Array"
    assert "¶" not in html and "nav junk" not in html and "<h1" not in html
    assert anchors_mod.inline_heading_ids(html) == {
        anchors_mod._key("Job Array Examples"): "job-array-examples"}


def test_sections_and_their_text():
    _, html = _clean_article(PAGE)
    secs = split_sections(html)
    assert [(s.heading, s.heading_level) for s in secs] == [("Overview", 0), ("Job Array Examples", 2)]
    assert "submit many similar jobs" in secs[0].text and "--array=1-10" in secs[1].text
    assert "../gpus/index.html" in secs[0].hrefs
