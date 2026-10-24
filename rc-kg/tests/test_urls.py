"""URL normalization. This is the join key between a body link and a Page
node, so every rule here has a downstream consequence for LINKS_TO."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ingest.urls import classify, host_of, is_wp_api_url, normalize  # noqa: E402

SITE = "https://rc.northeastern.edu"


def test_absolute_url_is_unchanged_apart_from_the_trailing_slash():
    assert normalize(SITE + "/compute/") == SITE + "/compute"


def test_root_keeps_its_slash():
    assert normalize(SITE) == SITE + "/"
    assert normalize(SITE + "/") == SITE + "/"


def test_root_relative_link_is_resolved_against_the_site():
    assert normalize("/getting-help/") == SITE + "/getting-help"


def test_protocol_relative_link():
    assert normalize("//rc.northeastern.edu/x/") == SITE + "/x"


def test_http_is_upgraded_to_https():
    assert normalize("http://rc.northeastern.edu/x") == SITE + "/x"


def test_www_is_stripped():
    assert normalize("https://www.rc.northeastern.edu/x") == SITE + "/x"


def test_host_is_lowercased():
    assert normalize("https://RC.Northeastern.EDU/X") == SITE + "/X"


def test_path_case_is_preserved():
    """Hosts are case-insensitive, paths are not: lowercasing the path would
    invent URLs that 404."""
    assert normalize(SITE + "/CaseSensitive") == SITE + "/CaseSensitive"


def test_query_and_fragment_are_dropped():
    assert normalize(SITE + "/p/?utm_source=x#frag") == SITE + "/p"


def test_duplicate_slashes_collapse():
    assert normalize(SITE + "//a///b/") == SITE + "/a/b"


def test_fragment_only_link_is_not_a_url():
    assert normalize("#section") is None


def test_non_http_schemes_are_rejected():
    for bad in ("mailto:a@b.edu", "tel:+16175551212", "javascript:void(0)",
                "data:text/plain,x", "MAILTO:A@B.EDU"):
        assert normalize(bad) is None, bad


def test_bare_relative_link_is_refused_rather_than_guessed():
    """Resolving 'software/' needs the page it appeared on. Guessing it is
    site-root-relative would silently invent a wrong target."""
    assert normalize("software/") is None


def test_empty_input():
    assert normalize("") is None
    assert normalize(None) is None


def test_classify_internal_vs_external():
    assert classify(SITE + "/x") == ("internal", "rc.northeastern.edu")
    assert classify("https://rc-docs.northeastern.edu/y") == (
        "external", "rc-docs.northeastern.edu")


def test_rc_docs_is_external_even_though_it_is_northeastern():
    """Scope rule: only rc.northeastern.edu is ingested. rc-docs is the most
    linked external domain (80 links) and must not be followed."""
    kind, host = classify("https://rc-docs.northeastern.edu/welcome/")
    assert kind == "external"


def test_host_of_strips_www_and_port():
    assert host_of("https://www.example.com:8443/x") == "example.com"


def test_wp_api_urls_are_recognised():
    assert is_wp_api_url(SITE + "/wp-json/wp/v2/pages")
    assert not is_wp_api_url(SITE + "/compute")


def test_normalization_is_idempotent():
    once = normalize(SITE + "//Compute/?a=1#b")
    assert normalize(once) == once
