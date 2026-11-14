"""Hunter Domain Search and account (design item 45). Never calls Email Finder.

Hunter's codes are not the usual ones (docs/vendor_notes.md): 429 means the usage limit is reached
(`HunterQuotaError`, never retried) and 403 means the rate limit (retried, honouring `retry-after`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import httpx
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from outreach.clients import HunterQuotaError, VendorAuthError, VendorUnavailableError

if TYPE_CHECKING:
    from outreach.config import Settings
    from outreach.db.runs_db import RunsDB

BASE_URL = "https://api.hunter.io/v2"
FREE_PLAN_LIMIT = 10  # limit + offset > 10 returns pagination_error on free plans


class _Retryable(Exception):
    def __init__(self, detail: str, retry_after: float | None = None) -> None:
        super().__init__(detail)
        self.detail, self.retry_after = detail, retry_after


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


class HunterClient:
    def __init__(self, api_key: str, settings: Settings, *, runs_db: RunsDB | None = None,
                 ids: Callable[[], str] | None = None, sleep: Callable[[float], None] | None = None,
                 http: httpx.Client | None = None, base_url: str = BASE_URL) -> None:
        self.api_key, self.settings = api_key, settings
        self.runs_db, self.ids, self.sleep = runs_db, ids, sleep
        self.http = http or httpx.Client(
            timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s),
            headers={"User-Agent": settings.user_agent})
        self.base_url = base_url.rstrip("/")

    def _get_once(self, path: str, params: dict[str, Any]) -> httpx.Response:
        try:
            response = self.http.get(f"{self.base_url}{path}", params={**params, "api_key": self.api_key})
        except (httpx.TransportError, httpx.TimeoutException) as e:
            raise _Retryable(type(e).__name__) from e
        status = response.status_code
        if status == 401:
            raise VendorAuthError("Hunter", 401, "HUNTER_API_KEY")
        if status == 429:
            raise HunterQuotaError("Hunter usage limit reached (429)")
        if status == 403 or status >= 500:
            raise _Retryable(f"HTTP {status}", _retry_after(response))
        if status >= 400:
            # e.g. 400 pagination_error: a bug here, not a vendor outage. The key is never in the message.
            raise RuntimeError(f"Hunter {path} returned HTTP {status}: {response.text[:300]}")
        return response

    def _wait(self, retry_state: Any) -> float:
        exc = retry_state.outcome.exception() if retry_state.outcome else None
        if isinstance(exc, _Retryable) and exc.retry_after is not None:
            return min(exc.retry_after, 60.0)
        return float(wait_exponential(multiplier=1, max=20)(retry_state))

    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response:
        kwargs: dict[str, Any] = {"sleep": self.sleep} if self.sleep is not None else {}
        retrying = Retrying(stop=stop_after_attempt(self.settings.vendor_retry_attempts), wait=self._wait,
                            retry=retry_if_exception(lambda e: isinstance(e, _Retryable)), reraise=True, **kwargs)
        try:
            return retrying(self._get_once, path, params)
        except _Retryable as e:
            raise VendorUnavailableError("Hunter", e.detail) from e

    def domain_search(self, domain: str, *, run_id: str | None = None) -> dict[str, Any]:
        response = self._get("/domain-search", {"domain": domain, "limit": FREE_PLAN_LIMIT})
        body: dict[str, Any] = response.json()
        emails = (body.get("data") or {}).get("emails") or []
        try:
            credits = float(response.headers["x-credits-used"])
        except (KeyError, ValueError):
            credits = float(self.settings.hunter_credits_per_call if emails else 0)  # charged only with results
        if self.runs_db is not None and self.ids is not None:
            self.runs_db.record_api_call(call_id=self.ids(), run_id=run_id, vendor="hunter",
                                         call_site="domain_search", units=credits)
        return body

    def account(self) -> dict[str, Any]:
        """GET /v2/account charges no credits."""
        result: dict[str, Any] = self._get("/account", {}).json()
        return result

    def credits_remaining(self) -> int | None:
        credits = ((self.account().get("data") or {}).get("requests") or {}).get("credits") or {}
        remaining = credits.get("remaining")
        return None if remaining is None else int(remaining)
