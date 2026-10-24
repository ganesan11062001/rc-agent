"""Topic matching: the only way a MENTIONS edge or a query topic is created."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrich.topics import _alias_pattern, load_topics, match_topics  # noqa: E402


def test_topics_file_loads_and_names_are_unique():
    topics = load_topics()
    assert len(topics) >= 20
    assert len({t.name for t in topics}) == len(topics)


def test_whole_word_only():
    p = _alias_pattern(["GPU", "OOD"])
    assert p.search("request a GPU node")
    assert not p.search("four GPUs")        # the plural is a separate alias
    assert not p.search("gpu-short")        # a partition name, not the word
    assert not p.search("the food court")


def test_paths_match_but_not_inside_longer_paths():
    p = _alias_pattern(["/scratch"])
    assert p.search("files in /scratch are purged")
    assert not p.search("see /home/user/scratchpad")


def test_case_insensitive_and_counts_hits():
    topics = load_topics()
    hits = match_topics("Job was OOM killed. out of memory again; seff shows 99%", topics)
    assert hits["Job failures and efficiency"] == 3


def test_generic_words_do_not_tag_outages():
    topics = load_topics()
    assert "Maintenance and outages" not in match_topics("use the drop down menu", topics)
    assert "Maintenance and outages" in match_topics("Explorer is down", topics)
