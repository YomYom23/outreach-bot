"""hunter_client: Hunter's own status codes (429 quota, 403 rate limit), limit=10, credits, api_calls."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from outreach.clients import HunterQuotaError, VendorAuthError, VendorUnavailableError
from outreach.clients.hunter_client import HunterClient
from outreach.config import Settings
from outreach.db.runs_db import RunsDB
from tests.fakes import SeqIds

FOUND = {"data": {"domain": "acme-roofing.test", "emails": [
    {"value": "pat@acme-roofing.test", "type": "personal", "seniority": "executive", "position": "Owner",
     "sources": [{"uri": "https://acme-roofing.test/about", "domain": "acme-roofing.test"}]}]}}


class Script:
    def __init__(self, replies: list[httpx.Response]) -> None:
        self.replies, self.requests = list(replies), []  # type: list[httpx.Response], list[httpx.Request]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.replies.pop(0)


def make(settings: Settings, replies: list[httpx.Response], runs_db: RunsDB | None = None
         ) -> tuple[HunterClient, Script, list[float]]:
    script, slept = Script(replies), []  # type: Script, list[float]
    http = httpx.Client(transport=httpx.MockTransport(script))
    return (HunterClient("hk", settings, runs_db=runs_db, ids=SeqIds(), sleep=slept.append, http=http),
            script, slept)


def test_domain_search_request_and_credits(settings: Settings, runs_db: RunsDB) -> None:
    hunter, script, _ = make(settings, [httpx.Response(200, json=FOUND, headers={"X-Credits-Used": "1"})], runs_db)
    body = hunter.domain_search("acme-roofing.test", run_id="R1")
    assert body["data"]["emails"][0]["sources"][0]["uri"] == "https://acme-roofing.test/about"
    [req] = script.requests
    assert req.url.path == "/v2/domain-search"
    assert dict(req.url.params) == {"domain": "acme-roofing.test", "limit": "10", "api_key": "hk"}
    [call] = runs_db.api_calls("R1")
    assert (call["vendor"], call["call_site"], call["units"]) == ("hunter", "domain_search", 1.0)


def test_empty_result_charges_nothing(settings: Settings, runs_db: RunsDB) -> None:
    hunter, _, _ = make(settings, [httpx.Response(200, json={"data": {"emails": []}})], runs_db)
    hunter.domain_search("none.test", run_id="R1")
    assert runs_db.api_calls("R1")[0]["units"] == 0


def test_429_is_quota_and_never_retried(settings: Settings) -> None:
    hunter, script, _ = make(settings, [httpx.Response(429, json={"errors": [{"details": "usage limit"}]})])
    with pytest.raises(HunterQuotaError):
        hunter.domain_search("acme-roofing.test")
    assert len(script.requests) == 1


def test_403_is_rate_limit_and_retried_with_retry_after(settings: Settings) -> None:
    hunter, script, slept = make(settings, [httpx.Response(403, headers={"Retry-After": "2"}),
                                            httpx.Response(200, json=FOUND)])
    assert hunter.domain_search("acme-roofing.test")["data"]["emails"]
    assert len(script.requests) == 2 and slept == [2.0]


def test_exhausted_5xx_becomes_unavailable(settings: Settings) -> None:
    hunter, script, _ = make(settings, [httpx.Response(502)] * settings.vendor_retry_attempts)
    with pytest.raises(VendorUnavailableError, match="Hunter"):
        hunter.domain_search("x.test")
    assert len(script.requests) == settings.vendor_retry_attempts


def test_connection_errors_are_retried(settings: Settings) -> None:
    calls: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=FOUND)

    http = httpx.Client(transport=httpx.MockTransport(flaky))
    hunter = HunterClient("hk", settings, sleep=lambda _s: None, http=http)
    assert hunter.domain_search("acme-roofing.test")["data"]["emails"]
    assert len(calls) == 2


def test_401_is_auth_error(settings: Settings) -> None:
    hunter, _, _ = make(settings, [httpx.Response(401)])
    with pytest.raises(VendorAuthError, match="HUNTER_API_KEY"):
        hunter.domain_search("x.test")


def test_other_4xx_does_not_leak_the_key(settings: Settings) -> None:
    hunter, _, _ = make(settings, [httpx.Response(400, json={"errors": [{"id": "pagination_error"}]})])
    with pytest.raises(RuntimeError) as info:
        hunter.domain_search("x.test")
    assert "pagination_error" in str(info.value) and "hk" not in str(info.value).replace("hunter", "")


def test_account_reads_remaining_credits(settings: Settings) -> None:
    account: dict[str, Any] = {"data": {"requests": {"credits": {"used": 3, "available": 25, "remaining": 22}}}}
    hunter, script, _ = make(settings, [httpx.Response(200, json=account)])
    assert hunter.credits_remaining() == 22
    assert script.requests[0].url.path == "/v2/account"
