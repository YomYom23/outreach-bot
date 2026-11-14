"""choose_links (design item 35). Lenient on purpose: any sensible score_link passes."""

from outreach.research.links import FALLBACK_PATHS, candidate_links, choose_links, score_link
from tests.conftest import load_profile
from tests.sites import site_page

KEYWORDS = load_profile().link_keywords


def acme_home() -> str:
    html = site_page("acme-roofing.test", "/")
    assert html is not None
    return html


def test_candidates_are_same_site_nav_links_without_home_or_offsite() -> None:
    paths = [p for p, _ in candidate_links(acme_home(), "https://acme-roofing.test/", "acme-roofing.test")]
    assert paths == ["/services", "/about-us", "/contact", "/blog", "/privacy-policy"]  # no "/", no facebook


def test_about_and_contact_beat_blog_and_privacy() -> None:
    chosen = choose_links(acme_home(), "https://acme-roofing.test/", "acme-roofing.test", KEYWORDS, 2)
    assert set(chosen) == {"/about-us", "/contact"}


def test_keyword_paths_score_above_zero_and_unrelated_paths_lower() -> None:
    for path, text in [("/about", "about"), ("/contact-us", "contact us"), ("/our-team", "meet the team"),
                       ("/our-story", "our story")]:
        assert score_link(path, text, KEYWORDS) > 0, path
    assert score_link("/contact", "contact", KEYWORDS) > score_link("/blog/2024/hail-damage", "blog", KEYWORDS)
    assert score_link("/contact", "contact", KEYWORDS) > score_link("/privacy-policy", "privacy", KEYWORDS)


def test_falls_back_when_nothing_scores() -> None:
    html = "<html><body><header><a href='/gallery'>Gallery</a></header></body></html>"
    if score_link("/gallery", "gallery", KEYWORDS) == 0:
        assert choose_links(html, "https://x.test/", "x.test", KEYWORDS, 3) == list(FALLBACK_PATHS)
    assert choose_links("<html></html>", "https://x.test/", "x.test", KEYWORDS, 1) == ["/contact"]
    assert choose_links(acme_home(), "https://acme-roofing.test/", "acme-roofing.test", KEYWORDS, 0) == []
