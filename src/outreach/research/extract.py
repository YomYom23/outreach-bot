"""Code-only extraction from raw HTML (design item 36, Rules: US evidence, Quote check).

Nothing here calls a model. Emails come from five methods: visible text, `mailto:` links, JSON-LD, Cloudflare's
`data-cfemail` encoding and `name [at] domain [dot] com` text. Hidden elements, scripts and styles are dropped
before the visible-text pass, so an address a visitor can't see is never "visible_text".
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import phonenumbers
from email_validator import EmailNotValidError, validate_email
from selectolax.lexbor import LexborHTMLParser, LexborNode

from outreach.models import DomainRelation, ExtractMethod

FREEMAIL = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "hotmail.com", "outlook.com", "live.com", "msn.com",
    "aol.com", "icloud.com", "me.com", "mac.com", "comcast.net", "att.net", "sbcglobal.net", "bellsouth.net",
    "verizon.net", "cox.net", "charter.net", "protonmail.com", "proton.me", "gmx.com", "mail.com", "zoho.com",
})

US_STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho",
    "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina",
    "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming", "DC": "District of Columbia",
}
_STATE_ALT = "|".join([*US_STATES, *(re.escape(n) for n in US_STATES.values())])
# A state followed by a ZIP code: "Tucson, AZ 85701" or "Austin, Texas 78701-1234".
PAGE_ADDRESS_RE = re.compile(rf"\b({_STATE_ALT}),?\s+(\d{{5}})(?:-\d{{4}})?\b")

EMAIL_RE = re.compile(r"(?<![\w.%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])")
OBFUSCATED_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)\s*[\[(\{]\s*at\s*[\])\}]\s*([A-Za-z0-9-]+(?:\s*[\[(\{]\s*dot\s*[\])\}]\s*"
    r"[A-Za-z0-9-]+)+)", re.IGNORECASE)
DOT_RE = re.compile(r"\s*[\[(\{]\s*dot\s*[\])\}]\s*", re.IGNORECASE)
CF_HREF_RE = re.compile(r"/cdn-cgi/l/email-protection#([0-9a-fA-F]+)")
HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿"), None)
QUOTES = str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                        "“": '"', "”": '"', "„": '"', "″": '"', " ": " ",
                        " ": " ", " ": " "})
FORM_FIELD_RE = re.compile(r"e-?mail|message|comment|inquir|enquir", re.IGNORECASE)


@dataclass(frozen=True)
class FoundEmail:
    address_norm: str
    address_as_seen: str
    method: ExtractMethod
    domain_relation: DomainRelation
    context: str


@dataclass
class PageExtract:
    emails: list[FoundEmail] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)  # E.164, valid US numbers only
    contact_form_url: str | None = None
    jsonld_addresses: list[str] = field(default_factory=list)
    us_evidence: str | None = None  # "address:<text>" or "phone:<e164>"
    markers: list[str] = field(default_factory=list)  # ai_tool_markers names
    title: str = ""


@dataclass(frozen=True)
class ExtractConfig:
    """The TargetProfile lists and settings extraction needs (kept small so tests can build one)."""

    file_extension_tlds: frozenset[str] = frozenset()
    placeholder_addresses: frozenset[str] = frozenset()
    vendor_denylist: tuple[str, ...] = ()
    ai_tool_markers: tuple[tuple[str, str], ...] = ()  # (name, regex)
    context_snippet_chars: int = 200
    testing: bool = False

    @classmethod
    def from_profile(cls, profile: Any, settings: Any) -> ExtractConfig:
        return cls(file_extension_tlds=frozenset(t.lower() for t in profile.file_extension_tlds),
                   placeholder_addresses=frozenset(a.lower() for a in profile.placeholder_addresses),
                   vendor_denylist=tuple(profile.vendor_denylist),
                   ai_tool_markers=tuple((m.name, m.html_pattern) for m in profile.ai_tool_markers),
                   context_snippet_chars=settings.context_snippet_chars, testing=settings.testing)


# --- text ------------------------------------------------------------------------


def normalize_for_match(text: str) -> str:
    """NFKC, straight quotes, no zero-width or non-breaking spaces, collapsed whitespace, casefolded."""
    text = unicodedata.normalize("NFKC", text).translate(ZERO_WIDTH).translate(QUOTES)
    return " ".join(text.split()).casefold()


def quote_found(quote: str | None, page_texts: Iterable[str]) -> bool:
    if not quote or not quote.strip():
        return False
    needle = normalize_for_match(quote)
    return any(needle in normalize_for_match(t) for t in page_texts)


def _is_hidden(node: LexborNode) -> bool:
    attrs = node.attributes
    return ("hidden" in attrs or attrs.get("aria-hidden") == "true"
            or bool(HIDDEN_STYLE_RE.search(attrs.get("style") or "")))


def parse(html: str) -> LexborHTMLParser:
    return LexborHTMLParser(html)


def visible_text(tree: LexborHTMLParser) -> str:
    """Text a visitor sees: no script, style, template, noscript or hidden elements. Works on a copy."""
    body = tree.body
    if body is None:
        return ""
    clone = LexborHTMLParser(body.html or "")
    for node in clone.css("script, style, template, noscript, svg, iframe"):
        node.decompose()
    for node in [n for n in clone.css("[hidden], [aria-hidden], [style]") if _is_hidden(n)]:
        node.decompose()
    root = clone.body
    return " ".join((root.text(separator=" ") if root else "").split())


def page_title(tree: LexborHTMLParser) -> str:
    node = tree.css_first("title")
    return " ".join(node.text().split()) if node else ""


# --- emails ----------------------------------------------------------------------


def cf_decode(hex_str: str) -> str | None:
    """Cloudflare email protection: the first byte is the key; every later byte is XORed with it."""
    try:
        raw = bytes.fromhex(hex_str)
    except ValueError:
        return None
    if len(raw) < 2:
        return None
    try:
        return bytes(b ^ raw[0] for b in raw[1:]).decode("utf-8")
    except UnicodeDecodeError:
        return None


def cf_encode(address: str, key: int = 0x5A) -> str:
    """The inverse of `cf_decode`; used by test fixtures."""
    return f"{key:02x}" + "".join(f"{b ^ key:02x}" for b in address.encode("utf-8"))


def domain_relation(address: str, domain_key: str) -> DomainRelation:
    host = address.rsplit("@", 1)[-1].lower()
    if host == domain_key or host.endswith("." + domain_key):
        return DomainRelation.same_domain
    if host in FREEMAIL:
        return DomainRelation.freemail
    return DomainRelation.other_domain


def accept_email(raw: str, cfg: ExtractConfig) -> str | None:
    """Returns the normalized address, or None for junk (file names, placeholders, vendor addresses)."""
    addr = raw.strip().strip(".,;:<>()[]\"'").lower()
    if "@" not in addr:
        return None
    host = addr.rsplit("@", 1)[1]
    if host.rsplit(".", 1)[-1] in cfg.file_extension_tlds or addr in cfg.placeholder_addresses:
        return None
    if any(host == v or host.endswith("." + v) for v in cfg.vendor_denylist):
        return None
    try:
        info = validate_email(addr, check_deliverability=False, test_environment=cfg.testing)
    except EmailNotValidError:
        return None
    return info.normalized.lower()


def _context(text: str, needle: str, chars: int) -> str:
    i = text.lower().find(needle.lower())
    if i < 0:
        return text[:chars]
    half = max(chars - len(needle), 0) // 2
    return text[max(i - half, 0): i + len(needle) + half][:chars]


def _jsonld_objects(tree: LexborHTMLParser) -> Iterator[dict]:
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text() or "")
        except ValueError:
            continue
        stack: list[Any] = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, dict):
                yield item
                stack.extend(item.values())
            elif isinstance(item, list):
                stack.extend(item)


def _jsonld_emails(objs: list[dict]) -> Iterator[str]:
    for obj in objs:
        value = obj.get("email")
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, str):
                yield v.removeprefix("mailto:")


def _jsonld_addresses(objs: list[dict]) -> list[str]:
    out = []
    for obj in objs:
        addr = obj.get("address")
        for a in addr if isinstance(addr, list) else [addr]:
            if isinstance(a, dict):
                parts = [a.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode",
                                            "addressCountry")]
                text = ", ".join(str(p) for p in parts if isinstance(p, str | int) and str(p).strip())
                if text:
                    out.append(text)
            elif isinstance(a, str) and a.strip():
                out.append(a.strip())
    return out


def _raw_emails(tree: LexborHTMLParser, text: str, objs: list[dict]) -> Iterator[tuple[str, ExtractMethod]]:
    for m in EMAIL_RE.finditer(text):
        yield m.group(0), ExtractMethod.visible_text
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if href.lower().startswith("mailto:"):
            yield unquote(href[7:].split("?", 1)[0]), ExtractMethod.mailto
        if (cf := CF_HREF_RE.search(href)) and (decoded := cf_decode(cf.group(1))):
            yield decoded, ExtractMethod.cf_decoded
    for node in tree.css("[data-cfemail]"):
        if decoded := cf_decode(node.attributes.get("data-cfemail") or ""):
            yield decoded, ExtractMethod.cf_decoded
    for v in _jsonld_emails(objs):
        yield v, ExtractMethod.jsonld
    for m in OBFUSCATED_RE.finditer(text):
        yield f"{m.group(1)}@{DOT_RE.sub('.', m.group(2))}", ExtractMethod.text_obfuscated


# --- page ------------------------------------------------------------------------


def us_phones(text: str) -> list[str]:
    out: list[str] = []
    for match in phonenumbers.PhoneNumberMatcher(text, "US"):
        n = match.number
        if phonenumbers.is_valid_number(n) and phonenumbers.region_code_for_number(n) == "US":
            e164 = phonenumbers.format_number(n, phonenumbers.PhoneNumberFormat.E164)
            if e164 not in out:
                out.append(e164)
    return out


def us_address_evidence(addresses: Iterable[str], text: str) -> str | None:
    for a in addresses:
        if re.search(rf"\b({_STATE_ALT})\b", a):
            return f"address:{a[:200]}"
    if found := PAGE_ADDRESS_RE.search(text):
        return f"address:{found.group(0)}"
    return None


def contact_form_url(tree: LexborHTMLParser, page_url: str) -> str | None:
    for form in tree.css("form"):
        fields = form.css("input, textarea")
        if any(f.tag == "textarea" or (f.attributes.get("type") or "").lower() == "email"
               or FORM_FIELD_RE.search((f.attributes.get("name") or "") + (f.attributes.get("id") or ""))
               for f in fields):
            return urljoin(page_url, form.attributes.get("action") or "") or page_url
    return None


def extract_page(html: str, page_url: str, domain_key: str, cfg: ExtractConfig) -> PageExtract:
    tree = parse(html)
    text = visible_text(tree)
    objs = list(_jsonld_objects(tree))
    out = PageExtract(title=page_title(tree))
    seen: set[tuple[str, ExtractMethod]] = set()
    for raw, method in _raw_emails(tree, text, objs):
        norm = accept_email(raw, cfg)
        if norm is None or (norm, method) in seen:
            continue
        seen.add((norm, method))
        out.emails.append(FoundEmail(norm, raw.strip(), method, domain_relation(norm, domain_key),
                                     _context(text, raw.strip(), cfg.context_snippet_chars)))
    out.phones = us_phones(text)
    out.jsonld_addresses = _jsonld_addresses(objs)
    out.us_evidence = us_address_evidence(out.jsonld_addresses, text) or (
        f"phone:{out.phones[0]}" if out.phones else None)
    out.contact_form_url = contact_form_url(tree, page_url)
    out.markers = [name for name, pattern in cfg.ai_tool_markers if re.search(pattern, html, re.IGNORECASE)]
    return out


def same_site(url: str, domain_key: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == domain_key or host.endswith("." + domain_key)
