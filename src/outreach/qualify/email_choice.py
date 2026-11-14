"""name_match and choose_email (design item 41; Decisions: Owner name match; Rules: Email categories,
Choosing an email, Greeting name).

Only on-site addresses (`same_domain`, `freemail`) and Hunter addresses with a source URL can be chosen.
Preference: an owner name match (`owner`), then a Hunter executive (`owner` only if it name-matches an owner,
else `general`), then a general address. Staff personal addresses are never chosen.

One addition to the design's "general address" rule (docs/vendor_notes.md, Phase 5): a local part that
contains the company's own name (`ridgelineaz@gmail.com` for "Ridgeline LLC") is a business address, so it
counts as general. Without this, a small roofer whose only address is a Gmail named after the business could
never be emailed.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from outreach.models import DomainRelation, EmailType, ExtractMethod, Person

ON_SITE = (DomainRelation.same_domain, DomainRelation.freemail)
# Most specific first, so "jim.carter" reports first.last rather than first.
FORM_ORDER = ("first.last", "first_last", "firstlast", "flast", "firstl", "last", "first")
EXEC_POSITION = re.compile(r"owner|founder|president|ceo", re.IGNORECASE)
NAME_SHAPED = re.compile(r"^[a-z]+[._][a-z]+$")  # first.last / first_last
COMPANY_FILLER = {"roofing", "roofers", "roofer", "roof", "roofs", "llc", "inc", "co", "company", "corp",
                  "the", "and", "of", "contractors", "contractor", "construction", "services", "ltd"}


def fold(text: str) -> str:
    """Lowercase, accents removed, hyphens and apostrophes removed."""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[-'’]", "", plain.lower())


def name_forms(full_name: str) -> dict[str, str]:
    tokens = [t for t in re.split(r"[^a-z]+", fold(full_name)) if t]
    if not tokens:
        return {}
    first = tokens[0]
    if len(tokens) == 1:
        return {"first": first}
    last = tokens[-1]
    return {"first.last": f"{first}.{last}", "first_last": f"{first}_{last}", "firstlast": first + last,
            "flast": first[0] + last, "firstl": first + last[0], "last": last, "first": first}


def is_owner(person: Person, ownership_words: Iterable[str]) -> bool:
    text = f"{person.role or ''} {person.quote}".lower()
    return any(w.lower() in text for w in ownership_words)


def owner_quote(person: Person, ownership_words: Iterable[str]) -> bool:
    return any(w.lower() in person.quote.lower() for w in ownership_words)


def name_match(local_part: str, people: Sequence[Person], ownership_words: Iterable[str]) -> tuple[Person, str] | None:
    """The owner whose name the local part exactly equals, and the form it matched. "Contains" never counts."""
    local = fold(local_part)
    words = tuple(ownership_words)
    for person in people:
        if not is_owner(person, words):
            continue
        forms = name_forms(person.name)
        for form in FORM_ORDER:
            if forms.get(form) == local:
                return person, form
    return None


def company_token(company_name: str | None) -> str | None:
    if not company_name:
        return None
    tokens = [t for t in re.split(r"[^a-z0-9]+", fold(company_name)) if t and t not in COMPANY_FILLER]
    token = "".join(tokens)
    return token if len(token) >= 4 else None


def is_general(local_part: str, role_local_parts: Iterable[str], company_name: str | None,
               people: Sequence[Person] = (), ownership_words: Iterable[str] = ()) -> bool:
    """On the role list, or carrying the company's name (and not a staff member's personal address)."""
    local = fold(local_part)
    if local in {r.lower() for r in role_local_parts}:
        return True
    token = company_token(company_name)
    return (token is not None and token in local.replace(".", "").replace("_", "")
            and not is_staff(local, people, ownership_words))


def is_staff(local_part: str, people: Sequence[Person], ownership_words: Iterable[str]) -> bool:
    """Shaped like a personal name, or matching a non-owner person."""
    local = fold(local_part)
    words = tuple(ownership_words)
    if any(local in name_forms(p.name).values() for p in people if not is_owner(p, words)):
        return True
    return bool(NAME_SHAPED.match(local))


@dataclass(frozen=True)
class EmailCandidate:
    address: str
    method: str
    domain_relation: str
    source_url: str
    hunter: dict | None = None

    @property
    def local(self) -> str:
        return self.address.split("@", 1)[0]

    @property
    def is_hunter(self) -> bool:
        return self.method == ExtractMethod.hunter

    @property
    def sourced(self) -> bool:
        if not self.is_hunter:
            return bool(self.source_url)
        return any(s.get("uri") for s in (self.hunter or {}).get("sources") or [])

    @classmethod
    def from_row(cls, row: Any) -> EmailCandidate:
        return cls(row["address_norm"], row["method"], row["domain_relation"], row["source_url"],
                   json.loads(row["hunter_json"]) if row["hunter_json"] else None)


@dataclass(frozen=True)
class EmailChoice:
    email: str
    email_type: EmailType
    source_kind: str  # "page" or "hunter"
    source_url: str
    greeting_name: str | None
    matched_form: str | None = None


def _rank(c: EmailCandidate) -> tuple[int, int]:
    return (0 if not c.is_hunter else 1, 0 if c.domain_relation == DomainRelation.same_domain else 1)


def choose_email(candidates: Sequence[EmailCandidate], people: Sequence[Person], *, ownership_words: Iterable[str],
                 role_local_parts: Iterable[str], company_name: str | None = None) -> EmailChoice | None:
    words, roles = tuple(ownership_words), tuple(role_local_parts)
    usable = sorted((c for c in candidates
                     if c.sourced and (c.is_hunter or c.domain_relation in ON_SITE)), key=_rank)

    def choice(c: EmailCandidate, kind: EmailType, greeting: str | None = None, form: str | None = None) -> EmailChoice:
        return EmailChoice(c.address, kind, "hunter" if c.is_hunter else "page", c.source_url, greeting, form)

    for c in usable:  # 1. an owner name match
        if (m := name_match(c.local, people, words)) is not None:
            person, form = m
            first = name_forms(person.name).get("first", "").title() or None
            greeting = None if form == "first" and not owner_quote(person, words) else first
            return choice(c, EmailType.owner, greeting, form)
    for c in usable:  # 2. a Hunter executive (no owner name match, or step 1 would have taken it)
        h = c.hunter or {}
        if c.is_hunter and (h.get("seniority") == "executive" or EXEC_POSITION.search(h.get("position") or "")):
            return choice(c, EmailType.general)
    for c in usable:  # 3. a general address
        hunter_generic = c.is_hunter and (c.hunter or {}).get("type") == "generic"
        if hunter_generic or (not c.is_hunter and is_general(c.local, roles, company_name, people, words)):
            return choice(c, EmailType.general)
    return None
