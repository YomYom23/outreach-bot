"""exa_client: request arguments pinned against the real exa-py, error mapping, retries, api_calls."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from outreach.clients import VendorAuthError, VendorUnavailableError
from outreach.clients.exa_client import MAX_EXCLUDE_DOMAINS, ExaClient
from outreach.config import Settings
from outreach.db.runs_db import RunsDB
from tests.fakes import SeqIds


class _Stub(BaseHTTPRequestHandler):
    replies: list[tuple[int, dict]] = []
    requests: list[tuple[str, dict, dict]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).requests.append((self.path, body, dict(self.headers)))
        status, payload = type(self).replies.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


@pytest.fixture
def stub() -> Iterator[str]:
    _Stub.replies, _Stub.requests = [], []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


OK = {"requestId": "r1", "results": [
    {"id": "1", "url": "https://acme-roofing.test/", "title": "Acme Roofing | Tucson",
     "highlights": ["Family owned since 1998"], "highlightScores": [0.9]},
    {"id": "2", "url": "https://www.yelp.test/biz/x", "title": None}],
    "costDollars": {"total": 0.007}}


def client(base_url: str, settings: Settings, runs_db: RunsDB | None = None, **kw: Any) -> ExaClient:
    return ExaClient("test-key", settings, base_url=base_url, runs_db=runs_db, ids=SeqIds(), sleep=lambda _s: None,
                     **kw)


def test_request_arguments_are_pinned(stub: str, settings: Settings, runs_db: RunsDB) -> None:
    _Stub.replies = [(200, OK)]
    blocklist = [f"d{i}.test" for i in range(MAX_EXCLUDE_DOMAINS + 5)]
    out = client(stub, settings, runs_db, exclude_domains=blocklist).search(
        "roofer Tucson", 25, objective="Roofer sites first; no directories.", run_id="R1")
    [(path, body, headers)] = _Stub.requests
    assert path == "/search" and headers["x-api-key"] == "test-key"
    assert body["query"] == "roofer Tucson" and body["numResults"] == 25 and body["type"] == "auto"
    assert body["objective"] == "Roofer sites first; no directories."
    assert body["category"] == "company"
    assert body["contents"] == {"highlights": True}
    assert len(body["excludeDomains"]) == MAX_EXCLUDE_DOMAINS
    assert not {"startPublishedDate", "endPublishedDate", "text"} & (set(body) | set(body["contents"]))
    assert out["results"][0] == {"url": "https://acme-roofing.test/", "title": "Acme Roofing | Tucson",
                                 "highlights": ["Family owned since 1998"]}
    assert out["results"][1]["title"] == "" and out["cost_dollars"]["total"] == 0.007
    [call] = runs_db.api_calls("R1")
    assert (call["vendor"], call["call_site"], call["units"], call["cost_estimate"]) == ("exa", "discovery", 1, 0.007)


def test_category_and_blocklist_can_be_left_out(stub: str, settings: Settings) -> None:
    _Stub.replies = [(200, OK)]
    client(stub, settings, category=None).search("q", 10)
    [(_, body, _)] = _Stub.requests
    assert not {"category", "excludeDomains", "objective"} & set(body)


def test_exa_ai_is_never_sent_as_an_exclude(stub: str, settings: Settings) -> None:
    """It stays on the local blocklist; sent to Exa it cut 10 results to 1-2 (phase 7 finding)."""
    _Stub.replies = [(200, OK)]
    client(stub, settings, exclude_domains=["yelp.com", "exa.ai", "angi.com"]).search("q", 10)
    [(_, body, _)] = _Stub.requests
    assert body["excludeDomains"] == ["yelp.com", "angi.com"]


@pytest.mark.parametrize("status", [401, 402])
def test_auth_errors(stub: str, settings: Settings, status: int) -> None:
    _Stub.replies = [(status, {"error": "bad key"})]
    with pytest.raises(VendorAuthError, match="EXA_API_KEY"):
        client(stub, settings).search("q", 10)
    assert len(_Stub.requests) == 1


def test_429_and_5xx_are_retried_then_succeed(stub: str, settings: Settings) -> None:
    _Stub.replies = [(429, {"error": "slow down"}), (503, {"error": "down"}), (200, OK)]
    assert len(client(stub, settings).search("q", 10)["results"]) == 2
    assert len(_Stub.requests) == 3


def test_exhausted_retries_become_unavailable(stub: str, settings: Settings) -> None:
    _Stub.replies = [(500, {})] * settings.vendor_retry_attempts
    with pytest.raises(VendorUnavailableError, match="Exa"):
        client(stub, settings).search("q", 10)
    assert len(_Stub.requests) == settings.vendor_retry_attempts


def test_bad_request_is_not_retried(stub: str, settings: Settings) -> None:
    _Stub.replies = [(400, {"error": "bad"})]
    with pytest.raises(ValueError, match="400"):
        client(stub, settings).search("q", 10)
    assert len(_Stub.requests) == 1


def test_missing_cost_uses_configured_price(stub: str, settings: Settings, runs_db: RunsDB) -> None:
    _Stub.replies = [(200, {"requestId": "r", "results": []})]
    client(stub, settings, runs_db).search("q", 10, run_id="R1")
    assert runs_db.api_calls("R1")[0]["cost_estimate"] == settings.exa_dollars_per_call
