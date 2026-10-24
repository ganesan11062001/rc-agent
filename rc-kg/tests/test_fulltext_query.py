"""The Lucene query built from ticket text."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from retrieval.graphrag import _fulltext_query  # noqa: E402


def test_compound_words_and_plurals_match_by_prefix():
    # "Jupyter" must reach "JupyterLab"; the index does not stem or split.
    assert _fulltext_query("start Jupyter") == "start OR start* OR Jupyter OR Jupyter*"


def test_short_terms_exact_and_special_characters_escaped():
    # 3-letter terms would prefix-match too much; "gpu-short" keeps its hyphen
    # (escaped for Lucene) and is matched exactly, not as a prefix.
    assert _fulltext_query("gpu gpu-short") == "gpu OR gpu\\-short"


def test_stopwords_and_duplicates_dropped():
    assert _fulltext_query("Hi, please help: the the GPU gpu") == "GPU"
