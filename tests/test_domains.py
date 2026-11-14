import pytest

from outreach.research.domains import Skip, normalize_domain

SHARED = ("wixsite.com", "github.io", "squarespace.com")
BLOCK = ("yelp.com", "angi.com")


@pytest.mark.parametrize(("url", "expected"), [
    ("https://acme-roofing.com", "acme-roofing.com"),
    ("https://WWW.Acme-Roofing.COM/contact?x=1#top", "acme-roofing.com"),
    ("acme-roofing.com/about", "acme-roofing.com"),
    ("http://blog.acme-roofing.com/post", "acme-roofing.com"),
    ("https://roofers.co.uk/a", "roofers.co.uk"),
    ("https://shop.acme.com.au", "acme.com.au"),
    ("https://acme-roofing.com.", "acme-roofing.com"),
])
def test_normalization_table(url: str, expected: str) -> None:
    assert normalize_domain(url, shared_host_patterns=SHARED, blocklist=BLOCK) == expected


def test_idna_hosts_become_punycode() -> None:
    assert normalize_domain("https://bücher-dach.de/kontakt") == "xn--bcher-dach-9db.de"


@pytest.mark.parametrize("url", ["https://joe.wixsite.com/roofing", "bob.github.io", "https://x.squarespace.com"])
def test_shared_hosts_skipped_by_suffix(url: str) -> None:
    out = normalize_domain(url, shared_host_patterns=SHARED)
    assert isinstance(out, Skip) and out.reason == "shared_host"


def test_private_suffix_not_in_patterns_is_its_own_domain() -> None:
    # Private PSL entries make joe.wixsite.com registrable; without a pattern it is kept as-is.
    assert normalize_domain("https://joe.wixsite.com", shared_host_patterns=()) == "joe.wixsite.com"


@pytest.mark.parametrize("url", ["https://www.yelp.com/biz/a", "https://m.yelp.com", "https://www.facebook.com/acme"])
def test_blocklisted_and_facebook_skipped(url: str) -> None:
    out = normalize_domain(url, blocklist=BLOCK)
    assert isinstance(out, Skip) and out.reason == "blocklisted"


@pytest.mark.parametrize(("url", "reason"), [("", "unparseable"), ("http://", "unparseable"),
                                               ("http://10.0.0.5/admin", "ip_address")])
def test_unusable_urls(url: str, reason: str) -> None:
    out = normalize_domain(url)
    assert isinstance(out, Skip) and out.reason == reason
