"""Owner name match (Decisions: Owner name match): exact forms only, owners only."""

import pytest

from outreach.models import Person
from outreach.qualify.email_choice import fold, name_forms, name_match
from tests.conftest import load_profile

WORDS = load_profile().ownership_words
JIM = Person(name="Jim Carter", role="owner", quote="Jim Carter, owner of Acme Roofing")


@pytest.mark.parametrize(("local", "form"), [
    ("jim", "first"), ("carter", "last"), ("jim.carter", "first.last"), ("jimcarter", "firstlast"),
    ("jim_carter", "first_last"), ("jcarter", "flast"), ("jimc", "firstl"), ("JIM.Carter", "first.last"),
])
def test_each_form(local: str, form: str) -> None:
    assert name_match(local, [JIM], WORDS) == (JIM, form)


@pytest.mark.parametrize("local", ["jimcarterroofing", "jim.carter2", "info", "jcart", "carterj", "j.carter"])
def test_contains_or_near_misses_never_match(local: str) -> None:
    assert name_match(local, [JIM], WORDS) is None


def test_accents_and_hyphens_are_folded() -> None:
    jose = Person(name="José Ruiz-Ortega", role="founder", quote="José founded it")
    assert fold("José Ruiz-Ortega") == "jose ruizortega"
    assert name_match("jose.ruizortega", [jose], WORDS) == (jose, "first.last")
    assert name_match("jruizortega", [jose], WORDS) == (jose, "flast")


def test_only_owners_match() -> None:
    dana = Person(name="Dana Carter", role="office manager", quote="Dana Carter runs the office")
    assert name_match("dana", [dana], WORDS) is None
    assert name_match("dana", [dana, JIM], WORDS) is None


def test_ownership_word_in_quote_is_enough() -> None:
    ben = Person(name="Ben Hollis", role=None, quote="Ben Hollis, owner and lead estimator")
    assert name_match("ben", [ben], WORDS) == (ben, "first")


def test_first_name_only_person() -> None:
    ben = Person(name="Ben", role="owner", quote="Ben (owner)")
    assert name_forms("Ben") == {"first": "ben"}
    assert name_match("ben", [ben], WORDS) == (ben, "first")
    assert name_match("bhollis", [ben], WORDS) is None


def test_most_specific_form_reported_first() -> None:
    # A two-token name whose first and last are equal would match several forms; the specific one wins.
    same = Person(name="Lee Lee", role="owner", quote="Lee Lee, owner")
    assert name_match("lee", [same], WORDS) == (same, "last")
