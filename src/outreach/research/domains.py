"""normalize_domain (design item 33): one key per business, used across runs as `domain_key` / `lead_id`.

tldextract runs offline on its bundled suffix snapshot. It does not lowercase, so this module does. With
private PSL entries on, `joe.wixsite.com` is its own registrable domain, so the shared-host skip tests the whole
host against `shared_host_patterns` (docs/vendor_notes.md, tldextract and Phase 4).
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlsplit

import tldextract

from outreach.db.memory_db import NORMALIZATION_VERSION

__all__ = ["NORMALIZATION_VERSION", "Skip", "normalize_domain"]

ALWAYS_SKIP = ("facebook.com",)


@dataclass(frozen=True)
class Skip:
    """Why a URL has no usable domain key."""

    reason: str  # "unparseable", "shared_host", "blocklisted", "ip_address"
    detail: str = ""


@lru_cache(maxsize=1)
def _extractor() -> tldextract.TLDExtract:
    # `test` is reserved (RFC 2606) and never resolves publicly; adding it lets fixture sites use the real path.
    return tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True,
                                 extra_suffixes=("test",))


def _host(url: str) -> str:
    text = url.strip()
    if "://" not in text:
        text = "http://" + text
    host = (urlsplit(text).hostname or "").strip(".").lower()
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""


def _matches(domain: str, patterns: Iterable[str]) -> bool:
    return any(domain == p or domain.endswith("." + p) for p in patterns)


def normalize_domain(url: str, *, shared_host_patterns: Iterable[str] = (),
                     blocklist: Iterable[str] = ()) -> str | Skip:
    """Returns the registrable domain (`acme-roofing.com`) or a `Skip`."""
    host = _host(url)
    if not host:
        return Skip("unparseable", url[:200])
    if host.startswith("www."):
        host = host[4:]
    ext = _extractor()(host)
    if not ext.suffix:
        return Skip("ip_address" if ext.domain.replace(".", "").isdigit() else "unparseable", host)
    # Matched against the whole host, not the registrable domain: private PSL entries make joe.wixsite.com
    # registrable, and builders missing from that list (x.squarespace.com) would merge into one key.
    if (shared := next((p for p in shared_host_patterns if host.endswith("." + p)), None)) is not None:
        return Skip("shared_host", shared)
    key = ext.top_domain_under_public_suffix or ext.fqdn
    if _matches(key, (*ALWAYS_SKIP, *blocklist)):
        return Skip("blocklisted", key)
    return key
