"""The fetcher's guards and status rules (design item 34), over the fixture sites."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from outreach.deps import Deps
from outreach.models import FetchStatus
from outreach.research.fetcher import Fetcher, is_public
from tests.conftest import make_settings
from tests.sites import PUBLIC_IP, SiteServer, make_fetcher

RUN = "R1"


@pytest.fixture
def server() -> SiteServer:
    return SiteServer()


@pytest.fixture
def fetcher(deps_fake: Deps, server: SiteServer) -> Fetcher:
    return make_fetcher(deps_fake, server)


def redirect(to: str, status: int = 301) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _r: httpx.Response(status, headers={"location": to})


def test_ok_page_is_stored_and_read_back(fetcher: Fetcher, server: SiteServer, deps_fake: Deps) -> None:
    page = fetcher.fetch_page(RUN, "acme-roofing.test", "/")
    assert page.ok and page.title.startswith("Acme Roofing") and "family-owned roofer" in page.clean_text
    again = fetcher.fetch_page(RUN, "acme-roofing.test", "/")
    assert again.page_id == page.page_id and again.html == page.html and again.clean_text == page.clean_text
    assert server.hits()["https://acme-roofing.test/"] == 1  # the second read came from `pages`
    row = deps_fake.runs_db.page_get(RUN, "https://acme-roofing.test/")
    assert row is not None and row["fetch_status"] == "ok" and row["raw_html_zlib"]


def test_url_is_built_from_the_claimed_domain_and_path_only(fetcher: Fetcher, server: SiteServer) -> None:
    fetcher.fetch_page(RUN, "acme-roofing.test", "contact")
    assert server.requests == ["https://acme-roofing.test/contact"]


def test_private_ip_on_first_hop(deps_fake: Deps, server: SiteServer) -> None:
    for ip in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "192.168.0.10", "::1"):
        f = make_fetcher(deps_fake, server, resolver=lambda _h, ip=ip: [ip])
        assert f.fetch_page(f"run-{ip}", "acme-roofing.test").fetch_status == FetchStatus.private_ip
    assert server.requests == []  # nothing was ever sent


def test_mixed_public_and_private_answers_are_refused(deps_fake: Deps, server: SiteServer) -> None:
    f = make_fetcher(deps_fake, server, resolver=lambda _h: [PUBLIC_IP, "10.0.0.1"])
    assert f.fetch_page(RUN, "acme-roofing.test").fetch_status == FetchStatus.private_ip


def test_private_ip_on_redirect_hop(deps_fake: Deps, server: SiteServer) -> None:
    server.overrides["https://acme-roofing.test/"] = redirect("https://internal.acme-roofing.test/")
    f = make_fetcher(deps_fake, server,
                     resolver=lambda h: ["10.0.0.7"] if h.startswith("internal.") else [PUBLIC_IP])
    page = f.fetch_page(RUN, "acme-roofing.test")
    assert page.fetch_status == FetchStatus.private_ip
    assert server.requests == ["https://acme-roofing.test/"]


def test_offsite_redirect(fetcher: Fetcher, server: SiteServer) -> None:
    server.overrides["https://acme-roofing.test/"] = redirect("https://evil.test/phish")
    page = fetcher.fetch_page(RUN, "acme-roofing.test")
    assert page.fetch_status == FetchStatus.offsite_redirect and page.final_url == "https://evil.test/phish"
    assert "https://evil.test/phish" not in server.requests


def test_same_site_redirect_is_followed(fetcher: Fetcher, server: SiteServer) -> None:
    server.overrides["https://acme-roofing.test/"] = redirect("https://www.acme-roofing.test/home", 302)
    server.overrides["https://www.acme-roofing.test/home"] = lambda _r: httpx.Response(
        200, text=open_fixture("acme-roofing.test"), headers={"content-type": "text/html"})
    page = fetcher.fetch_page(RUN, "acme-roofing.test")
    assert page.ok and page.final_url == "https://www.acme-roofing.test/home"


def test_redirect_loop_stops_at_max_redirects(fetcher: Fetcher, server: SiteServer, deps_fake: Deps) -> None:
    server.overrides["https://acme-roofing.test/"] = redirect("https://acme-roofing.test/")
    page = fetcher.fetch_page(RUN, "acme-roofing.test")
    assert page.fetch_status == FetchStatus.http_error
    assert len(server.requests) == deps_fake.settings.max_redirects + 1


@pytest.mark.parametrize("status", [403, 503])
def test_403_and_503_are_blocked(fetcher: Fetcher, server: SiteServer, status: int) -> None:
    server.overrides["https://acme-roofing.test/"] = lambda _r: httpx.Response(status, text="no")
    assert fetcher.fetch_page(RUN, "acme-roofing.test").fetch_status == FetchStatus.blocked


def test_challenge_page_is_blocked(fetcher: Fetcher) -> None:
    assert fetcher.fetch_page(RUN, "challenge.test").fetch_status == FetchStatus.blocked


def test_js_shell(fetcher: Fetcher) -> None:
    assert fetcher.fetch_page(RUN, "js-shell.test").fetch_status == FetchStatus.js_shell


def test_soft_404(fetcher: Fetcher) -> None:
    assert fetcher.fetch_page(RUN, "soft404-roofing.test", "/").ok
    assert fetcher.fetch_page(RUN, "soft404-roofing.test", "/contact").fetch_status == FetchStatus.soft_404


def test_404_and_non_html_are_http_errors(fetcher: Fetcher, server: SiteServer) -> None:
    assert fetcher.fetch_page(RUN, "acme-roofing.test", "/nope").fetch_status == FetchStatus.http_error
    server.overrides["https://acme-roofing.test/logo"] = lambda _r: httpx.Response(
        200, content=b"\x89PNG", headers={"content-type": "image/png"})
    assert fetcher.fetch_page(RUN, "acme-roofing.test", "/logo").fetch_status == FetchStatus.http_error


def test_5xx_retried_then_http_error(fetcher: Fetcher, server: SiteServer, deps_fake: Deps) -> None:
    server.overrides["https://acme-roofing.test/"] = lambda _r: httpx.Response(502)
    page = fetcher.fetch_page(RUN, "acme-roofing.test")
    assert page.fetch_status == FetchStatus.http_error and page.http_status == 502
    assert len(server.requests) == deps_fake.settings.vendor_retry_attempts


def test_timeout(fetcher: Fetcher, server: SiteServer) -> None:
    def slow(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    server.overrides["https://acme-roofing.test/"] = slow
    server.overrides["http://acme-roofing.test/"] = slow
    assert fetcher.fetch_page(RUN, "acme-roofing.test").fetch_status == FetchStatus.timeout


def test_https_connect_failure_falls_back_to_http(fetcher: Fetcher, server: SiteServer) -> None:
    def refuse(_r: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no TLS here")

    server.overrides["https://acme-roofing.test/"] = refuse
    page = fetcher.fetch_page(RUN, "acme-roofing.test")
    assert page.ok and page.final_url == "http://acme-roofing.test/"


def test_body_is_capped(deps_fake: Deps, server: SiteServer, tmp_path: Path) -> None:
    small = make_settings(tmp_path, max_page_bytes=1000)
    f = Fetcher(small, deps_fake.runs_db, deps_fake.clock, resolver=lambda _h: [PUBLIC_IP],
                transport=server.transport)
    page = f.fetch_page(RUN, "acme-roofing.test")
    assert len(page.html.encode()) <= 1000


def test_per_host_delay(fetcher: Fetcher, deps_fake: Deps) -> None:
    fetcher.fetch_page(RUN, "acme-roofing.test", "/")
    fetcher.fetch_page(RUN, "acme-roofing.test", "/contact")
    fetcher.fetch_page(RUN, "ortega-roofing.test", "/")
    assert deps_fake.clock.slept == [deps_fake.settings.per_host_delay_s]  # type: ignore[attr-defined]


def test_is_public() -> None:
    assert is_public(PUBLIC_IP) and is_public("2606:4700::1111")
    assert not any(is_public(ip) for ip in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "169.254.1.1", "0.0.0.0",
                                            "fe80::1", "fc00::1", "224.0.0.1"))


def open_fixture(domain: str, name: str = "index") -> str:
    from tests.sites import SITES

    return (SITES / domain / f"{name}.html").read_text(encoding="utf-8")
