"""choose_email (Rules: Email categories, Choosing an email, Greeting name)."""

import json

from outreach.models import EmailType, Person
from outreach.qualify.email_choice import EmailCandidate, choose_email
from tests.conftest import load_profile
from tests.sites import FIXTURES

PROFILE = load_profile()
JIM = Person(name="Jim Carter", role="owner", quote="Jim Carter, owner of Acme Roofing")
DANA = Person(name="Dana Carter", role="office manager", quote="Dana Carter runs the office")


def page(address: str, relation: str = "same_domain", method: str = "visible_text") -> EmailCandidate:
    return EmailCandidate(address, method, relation, "https://acme.test/contact")


def hunter(entry: dict, relation: str = "same_domain") -> EmailCandidate:
    uri = (entry.get("sources") or [{}])[0].get("uri", "") if entry.get("sources") else ""
    return EmailCandidate(entry["value"], "hunter", relation, uri, entry)


def hunter_fixture(name: str) -> list[dict]:
    return json.loads((FIXTURES / "hunter" / f"{name}.json").read_text(encoding="utf-8"))["data"]["emails"]


def choose(cands: list[EmailCandidate], people: list[Person] | None = None, company: str | None = "Acme Roofing"):
    return choose_email(cands, people if people is not None else [JIM, DANA],
                        ownership_words=PROFILE.ownership_words, role_local_parts=PROFILE.role_local_parts,
                        company_name=company)


def test_owner_match_beats_general() -> None:
    c = choose([page("info@acme.test"), page("jim@acme.test", method="mailto")])
    assert c is not None
    assert (c.email, c.email_type, c.greeting_name, c.matched_form) == (
        "jim@acme.test", EmailType.owner, "Jim", "first")
    assert c.source_kind == "page"


def test_general_when_no_owner_match() -> None:
    c = choose([page("info@acme.test")])
    assert (c.email, c.email_type, c.greeting_name) == ("info@acme.test", EmailType.general, None)


def test_staff_personal_addresses_never_chosen() -> None:
    assert choose([page("dana@acme.test")]) is None  # matches a non-owner person
    assert choose([page("mike.jones@acme.test")]) is None  # shaped like a name
    assert choose([page("dana@acme.test"), page("office@acme.test")]).email == "office@acme.test"


def test_other_domain_never_chosen() -> None:
    assert choose([page("jim@supplier.test", relation="other_domain")]) is None


def test_freemail_owner_is_chosen() -> None:
    c = choose([page("jimcarter@gmail.com", relation="freemail")])
    assert (c.email, c.email_type, c.matched_form) == ("jimcarter@gmail.com", EmailType.owner, "firstlast")


def test_business_named_freemail_is_general() -> None:
    """Live finding: Ridgeline's only address is ridgelineaz@gmail.com, named after the business."""
    c = choose([page("ridgelineaz@gmail.com", relation="freemail")], people=[], company="Ridgeline LLC")
    assert c is not None and c.email_type == EmailType.general
    assert choose([page("randomname@gmail.com", relation="freemail")], people=[], company="Ridgeline LLC") is None


def test_same_domain_preferred_over_freemail_and_page_over_hunter() -> None:
    c = choose([page("info@gmail.com", relation="freemail"), page("info@acme.test")])
    assert c.email == "info@acme.test"
    [office, _] = hunter_fixture("generic")
    c = choose([hunter(office), page("sales@acme.test")])
    assert c.email == "sales@acme.test"


def test_hunter_executive_is_general_unless_owner_name_matches() -> None:
    [john, _crew] = hunter_fixture("executive")
    c = choose([hunter(john)], people=[])
    assert (c.email_type, c.source_kind, c.greeting_name) == (EmailType.general, "hunter", None)
    carver = Person(name="John Carver", role="Co-Owner", quote="John Carver, co-owner")
    c = choose([hunter(john)], people=[carver])
    assert (c.email_type, c.greeting_name) == (EmailType.owner, "John")


def test_hunter_generic_is_general_and_hunter_junior_is_not_chosen() -> None:
    office, pat = hunter_fixture("generic")
    assert choose([hunter(pat)], people=[]) is None
    assert choose([hunter(pat), hunter(office)], people=[]).email == "office@gen.test"


def test_unsourced_address_rejected() -> None:
    """G5: a Hunter address without a source URL is never chosen, even an executive or generic one."""
    assert choose([hunter(e) for e in hunter_fixture("unsourced")], people=[]) is None


def test_greeting_first_form_needs_an_ownership_quote() -> None:
    by_role_only = Person(name="Ben Hollis", role="owner", quote="Ben handles every estimate himself")
    c = choose([page("ben@acme.test")], people=[by_role_only])
    assert (c.email_type, c.greeting_name) == (EmailType.owner, None)  # "Hi there"
    c = choose([page("bhollis@acme.test")], people=[by_role_only])
    assert c.greeting_name == "Ben"  # a stronger form needs no quote
