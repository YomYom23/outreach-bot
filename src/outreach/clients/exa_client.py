"""Exa search (design item 44).

exa-py 2.28 deprecates `search_and_contents`, so this client calls `search()` with the request Exa's
build-with-exa skill recommends: `type="auto"`, `contents={"highlights": True}` (Exa sizes each excerpt;
`snippet_chars` is enforced in code), and an `objective` on every call, because each search is one step
of a larger task. `num_results`, `category="company"` and the blocklist in `exclude_domains` are design
decisions (docs/vendor_notes.md, Exa and Phase 3b).

exa-py raises a plain `ValueError("Request failed with status code NNN: ...")` for HTTP errors, so the
status is parsed from the message. It exposes no response headers, so `retry-after` can't be honoured.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

from outreach.clients import VendorAuthError, VendorUnavailableError

if TYPE_CHECKING:
    from outreach.config import Settings
    from outreach.db.runs_db import RunsDB

MAX_EXCLUDE_DOMAINS = 1200
# Dropped locally by normalize_domain, but never sent: with "exa.ai" in excludeDomains, Exa returned 1-2
# results instead of 10 for the same query (phase 7 live smoke, 2026-11-13; vendor_notes.md).
NEVER_SENT_EXCLUDES = frozenset({"exa.ai"})
# The skill's recommended description for an LLM tool's `objective` parameter, used verbatim (phase 4).
OBJECTIVE_DESCRIPTION = ("Goal for this search turn; say which documents should rank first, which should be "
                         "excluded, and what specific facts or figures to pull from them.")
_STATUS = re.compile(r"status code (\d{3})")


class _Retryable(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def http_status(exc: BaseException) -> int | None:
    m = _STATUS.search(str(exc))
    return int(m.group(1)) if m else None


def _is_connection_error(exc: BaseException) -> bool:
    import requests
    from exa_py.errors import ExaTimeoutError

    return isinstance(exc, ExaTimeoutError | requests.exceptions.ConnectionError | requests.exceptions.Timeout)


def search_kwargs(num_results: int, exclude_domains: list[str], objective: str | None,
                  category: str | None) -> dict[str, Any]:
    """The exact arguments sent to `Exa.search` (pinned by test_exa_client)."""
    kwargs: dict[str, Any] = {
        "type": "auto",
        "num_results": num_results,
        "contents": {"highlights": True},
    }
    if objective:
        kwargs["objective"] = objective
    if category:
        kwargs["category"] = category  # "company" rejects published-date filters, so none are sent
    sent = [d for d in exclude_domains if d not in NEVER_SENT_EXCLUDES]
    if sent:
        kwargs["exclude_domains"] = sent[:MAX_EXCLUDE_DOMAINS]
    return kwargs


def _to_dict(response: Any) -> dict[str, Any]:
    results = []
    for r in getattr(response, "results", None) or []:
        results.append({"url": r.url, "title": getattr(r, "title", None) or "",
                        "highlights": list(getattr(r, "highlights", None) or [])})
    cost = getattr(response, "cost_dollars", None)
    return {"results": results, "cost_dollars": {"total": getattr(cost, "total", None)}}


class ExaClient:
    """`search(query, num_results)` -> {"results": [{url, title, highlights}], "cost_dollars": {"total"}}."""

    def __init__(self, api_key: str, settings: Settings, *, exclude_domains: list[str] | None = None,
                 runs_db: RunsDB | None = None, ids: Callable[[], str] | None = None,
                 sleep: Callable[[float], None] | None = None, sdk: Any = None,
                 base_url: str = "https://api.exa.ai", category: str | None = "company") -> None:
        if sdk is None:
            from exa_py import Exa

            sdk = Exa(api_key, base_url=base_url, timeout=settings.connect_timeout_s + settings.read_timeout_s)
        self.sdk, self.settings = sdk, settings
        self.exclude_domains = list(exclude_domains or [])
        self.runs_db, self.ids = runs_db, ids
        self.sleep = sleep
        self.category = category

    def _call(self, query: str, num_results: int, objective: str | None) -> Any:
        try:
            return self.sdk.search(query, **search_kwargs(num_results, self.exclude_domains, objective,
                                                          self.category))
        except ValueError as e:
            status = http_status(e)
            if status in (401, 402):
                raise VendorAuthError("Exa", status, "EXA_API_KEY") from e
            if status is not None and (status == 429 or status >= 500):
                raise _Retryable(f"HTTP {status}") from e
            raise
        except Exception as e:
            if _is_connection_error(e):
                raise _Retryable(type(e).__name__) from e
            raise

    def search(self, query: str, num_results: int, *, objective: str | None = None, run_id: str | None = None,
               call_site: str = "discovery") -> dict[str, Any]:
        """`objective` is forwarded unchanged; when an LLM picks the query, it writes the objective too."""
        retry_kwargs: dict[str, Any] = {}
        if self.sleep is not None:
            retry_kwargs["sleep"] = self.sleep
        retrying = Retrying(stop=stop_after_attempt(self.settings.vendor_retry_attempts),
                            wait=wait_exponential(multiplier=1, max=20),
                            retry=retry_if_exception(lambda e: isinstance(e, _Retryable)), reraise=True,
                            **retry_kwargs)
        try:
            response = retrying(self._call, query, num_results, objective)
        except _Retryable as e:
            raise VendorUnavailableError("Exa", e.detail) from e
        out = _to_dict(response)
        if self.runs_db is not None and self.ids is not None:
            total = out["cost_dollars"]["total"]
            self.runs_db.record_api_call(
                call_id=self.ids(), run_id=run_id, vendor="exa", call_site=call_site, units=1,
                cost_estimate=total if total is not None else self.settings.exa_dollars_per_call)
        return out
