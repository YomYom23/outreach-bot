"""fetch_page (design item 34). Not a model tool: code picks every URL, built from the claimed domain.

Guards, per hop:
* the host must resolve only to public addresses (`private_ip` otherwise). The DNS-rebinding window between
  this check and httpx's own connect is accepted (Alternative O);
* a redirect must stay on the claimed domain or its subdomains (`offsite_redirect`);
* at most `max_redirects` hops, `max_page_bytes` read, `text/html` only.

Status rules: 403/503, a "Just a moment..." title or a `cf-chl` marker is `blocked`; visible text shorter than
`js_shell_min_chars` is `js_shell`; a non-home page whose clean text hashes like the home page is `soft_404`.
Every page, good or bad, is written to `pages` and read back from there on a re-run.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlsplit

import httpx
from tenacity import RetryError, Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from outreach.models import FetchStatus
from outreach.research.extract import page_title, parse, same_site, visible_text

if TYPE_CHECKING:
    from outreach.config import Settings
    from outreach.db.runs_db import RunsDB
    from outreach.deps import Clock

Resolver = Callable[[str], list[str]]
RETRY_STATUSES = frozenset({500, 502, 504})
BLOCK_STATUSES = frozenset({403, 503})


def system_resolver(host: str) -> list[str]:
    return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})


def is_public(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%", 1)[0])
    return not (addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
                or addr.is_multicast or addr.is_unspecified)


@dataclass
class PageRecord:
    page_id: int
    requested_url: str
    fetch_status: FetchStatus
    final_url: str | None = None
    http_status: int | None = None
    html: str = ""
    clean_text: str = ""
    sha256: str | None = None
    title: str = ""

    @property
    def ok(self) -> bool:
        return self.fetch_status == FetchStatus.ok

    @property
    def path(self) -> str:
        return urlsplit(self.requested_url).path or "/"


@dataclass
class _Outcome:
    status: FetchStatus
    final_url: str | None = None
    http_status: int | None = None
    content_type: str | None = None
    body: bytes = b""


class _Retry(Exception):
    pass


@dataclass
class Fetcher:
    settings: Settings
    runs_db: RunsDB
    clock: Clock
    resolver: Resolver = system_resolver
    transport: httpx.BaseTransport | None = None
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _last_hit: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    # --- public -----------------------------------------------------------------------

    def fetch_page(self, run_id: str, domain_key: str, path: str = "/") -> PageRecord:
        path = path if path.startswith("/") else "/" + path
        requested = f"https://{domain_key}{path}"
        cached = self.runs_db.page_get(run_id, requested)
        if cached is not None:
            return self._record(cached)
        outcome = self._fetch(domain_key, path)
        html = outcome.body.decode("utf-8", errors="replace") if outcome.body else ""
        status, text, digest, title = outcome.status, "", None, ""
        if status == FetchStatus.ok:
            tree = parse(html)
            text, title = visible_text(tree), page_title(tree)
            digest = hashlib.sha256(text.encode()).hexdigest()
            status = self._classify(run_id, domain_key, path, html, text, title, digest)
        page_id = self.runs_db.page_put(
            run_id=run_id, lead_id=domain_key, requested_url=requested, fetch_status=status,
            final_url=outcome.final_url, http_status=outcome.http_status, content_type=outcome.content_type,
            raw_html_zlib=zlib.compress(outcome.body) if outcome.body else None, clean_text=text or None,
            sha256=digest)
        return PageRecord(page_id, requested, status, outcome.final_url, outcome.http_status, html, text, digest,
                          title)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()

    # --- internals --------------------------------------------------------------------

    def _record(self, row: Any) -> PageRecord:
        html = zlib.decompress(row["raw_html_zlib"]).decode("utf-8", errors="replace") if row["raw_html_zlib"] else ""
        return PageRecord(row["page_id"], row["requested_url"], FetchStatus(row["fetch_status"]), row["final_url"],
                          row["http_status"], html, row["clean_text"] or "", row["sha256"],
                          page_title(parse(html)) if html else "")

    def _classify(self, run_id: str, domain_key: str, path: str, html: str, text: str, title: str,
                  digest: str) -> FetchStatus:
        if title.strip().lower().startswith("just a moment") or "cf-chl" in html:
            return FetchStatus.blocked
        if len(text) < self.settings.js_shell_min_chars:
            return FetchStatus.js_shell
        if path != "/":
            home = self.runs_db.page_get(run_id, f"https://{domain_key}/")
            if home is not None and home["sha256"] == digest:
                return FetchStatus.soft_404
        return FetchStatus.ok

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            s = self.settings
            self._client = httpx.Client(
                transport=self.transport, follow_redirects=False, headers={"User-Agent": s.user_agent},
                timeout=httpx.Timeout(s.read_timeout_s, connect=s.connect_timeout_s))
        return self._client

    def _pace(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.settings.per_host_delay_s - (self.clock.monotonic() - last)
            if wait > 0:
                self.clock.sleep(wait)
        self._last_hit[host] = self.clock.monotonic()

    def _fetch(self, domain_key: str, path: str) -> _Outcome:
        outcome = self._follow(f"https://{domain_key}{path}", domain_key)
        if outcome.status in (FetchStatus.http_error, FetchStatus.timeout) and outcome.http_status is None:
            fallback = self._follow(f"http://{domain_key}{path}", domain_key)  # no TLS, or TLS broken
            if fallback.http_status is not None or fallback.status not in (FetchStatus.http_error,
                                                                          FetchStatus.timeout):
                return fallback
        return outcome

    def _follow(self, url: str, domain_key: str) -> _Outcome:
        last_status: int | None = None
        for _hop in range(self.settings.max_redirects + 1):
            host = (urlsplit(url).hostname or "").lower()
            if not same_site(url, domain_key):
                return _Outcome(FetchStatus.offsite_redirect, url)
            try:
                ips = self.resolver(host)
            except OSError:
                return _Outcome(FetchStatus.http_error, url)
            if not ips or not all(is_public(ip) for ip in ips):
                return _Outcome(FetchStatus.private_ip, url)
            try:
                outcome = self._get_with_retries(url, host)
            except httpx.TimeoutException:
                return _Outcome(FetchStatus.timeout, url)
            except (httpx.HTTPError, RetryError):
                return _Outcome(FetchStatus.http_error, url)
            if outcome.http_status is not None and 300 <= outcome.http_status < 400 and outcome.final_url:
                url, last_status = outcome.final_url, outcome.http_status
                continue
            return outcome
        return _Outcome(FetchStatus.http_error, url, last_status)  # too many redirects; the site did answer

    def _get_with_retries(self, url: str, host: str) -> _Outcome:
        retrying = Retrying(stop=stop_after_attempt(self.settings.vendor_retry_attempts),
                            wait=wait_exponential(multiplier=0.5, max=8), sleep=self.clock.sleep,
                            retry=retry_if_exception_type((_Retry, httpx.ConnectError, httpx.RemoteProtocolError)),
                            reraise=False)
        try:
            return retrying(self._get, url, host)
        except RetryError as e:
            last = e.last_attempt.exception()
            if isinstance(last, httpx.TimeoutException):
                raise last from e
            if isinstance(last, _Retry):
                return _Outcome(FetchStatus.http_error, url, int(str(last)))
            raise

    def _get(self, url: str, host: str) -> _Outcome:
        self._pace(host)
        with self.client.stream("GET", url) as r:
            if r.status_code in RETRY_STATUSES:
                raise _Retry(str(r.status_code))
            if r.is_redirect:
                location = r.headers.get("location")
                return _Outcome(FetchStatus.http_error, urljoin(url, location) if location else None, r.status_code)
            ctype = r.headers.get("content-type", "")
            if r.status_code in BLOCK_STATUSES:
                return _Outcome(FetchStatus.blocked, url, r.status_code, ctype)
            if r.status_code != 200:
                return _Outcome(FetchStatus.http_error, url, r.status_code, ctype)
            if "text/html" not in ctype.lower():
                return _Outcome(FetchStatus.http_error, url, r.status_code, ctype)
            body = bytearray()
            for chunk in r.iter_bytes():
                body.extend(chunk)
                if len(body) >= self.settings.max_page_bytes:
                    del body[self.settings.max_page_bytes:]
                    break
            return _Outcome(FetchStatus.ok, url, r.status_code, ctype, bytes(body))
