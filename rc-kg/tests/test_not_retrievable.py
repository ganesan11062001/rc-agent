"""People, leadership news and feed pages: kept in the graph, not retrieved."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.run import _mark_not_support, _person, feed_lead  # noqa: E402

SITE = "https://rc.northeastern.edu"
TEAM = SITE + "/research-computing-team"
AVP = SITE + "/from-the-avp"


def _row(sid, page, heading, retrievable=True):
    return {"section_id": sid, "page_url": page, "heading": heading, "retrievable": retrievable}


def test_people_pages_categories_and_bios_elsewhere_are_hidden():
    rows = [
        _row("t0", TEAM, "Overview"),
        _row("t1", TEAM, "Sam Rivera, PhD"),
        _row("t2", TEAM, "Robin (Rob) Chen, PhD"),
        _row("t3", TEAM, "Research Computing Student Alumni"),
        _row("a1", AVP, "Alex Morgan"),
        _row("h1", SITE, "Alex Morgan, PhD"),            # bio repeated on the home page
        _row("h2", SITE, "Explorer"),
        _row("s1", SITE + "/2026/03/01/spotlight", "Overview"),
        _row("g1", SITE + "/2026/03/02/gpu-tips", "Overview"),
        _row("f1", SITE + "/getting-help", "How Can Research Computing Support You?"),
    ]
    cats = {SITE + "/2026/03/01/spotlight": {"Researcher Spotlight", "Uncategorized"},
            SITE + "/2026/03/02/gpu-tips": {"Tech Tips"}}
    stats = _mark_not_support(rows, cats)
    hidden = {r["section_id"] for r in rows if not r["retrievable"]}

    assert hidden == {"t0", "t1", "t2", "t3", "a1", "h1", "s1"}
    assert stats == {"people_or_leadership_pages": 6, "bios_elsewhere": 1}


def test_person_headings():
    assert _person("Sam Rivera, PhD") == "sam rivera"
    assert _person("Alex Morgan") == _person("Alex Morgan, PhD")
    assert _person("Robin (Rob) Chen, PhD") == "robin (rob) chen"
    for not_a_name in ("Overview", "Research Computing Student Alumni", "Explorer",
                       "How Can Research Computing Support You?"):
        assert _person(not_a_name) is None
    # Shaped like a name, and harmless: only names the people pages introduce
    # hide a section, and no one is called "GPU Nodes".
    assert _person("GPU Nodes") == "gpu nodes"


def test_feed_keeps_only_its_lead():
    titles = ["Explorer and Discovery are Down", "Scratch Purge Scheduled for Tuesday, March 3, 2026",
              "Globus Maintenance: Monday, July 27", "Short"]
    text = ("The Explorer Cluster is fully operational. Contact rchelp@northeastern.edu.\n\n"
            "- Globus Maintenance: Monday, July 27 July 23, 2026 Globus will be down.\n"
            "- Explorer and Discovery are Down April 19, 2026 There was a power sag.\n"
            "- Scratch Purge Scheduled for Tuesday, March 3, 2026 February 27, 2026 Purge.")
    assert feed_lead(text, titles) == ("The Explorer Cluster is fully operational. "
                                       "Contact rchelp@northeastern.edu.")


def test_an_ordinary_list_is_not_a_feed():
    titles = ["Explorer and Discovery are Down", "Globus Maintenance: Monday, July 27"]
    text = ("Steps:\n- Explorer and Discovery are Down is a post\n- run sinfo\n- run squeue\n"
            "- Globus Maintenance: Monday, July 27 again")
    assert feed_lead(text, titles) is None     # only two bullets are post titles
