"""Hunter in the qualifier (design item 42; Error policy rows for Hunter)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from outreach.models import Person
from tests.qualify_support import QualifyEnv, hunter_fixture, qualify_env

OWNER = Person(name="John Carver", role="Co-Owner", quote="John Carver and Jordan Carver, co-owners")


@pytest.fixture
def qe(tmp_path: Path) -> Iterator[QualifyEnv]:
    with qualify_env(tmp_path) as env:
        yield env


def test_trigger_only_for_form_only_leads_at_or_above_n(qe: QualifyEnv) -> None:
    qe.add_lead("has-email.test", emails=[("info@has-email.test", "same_domain")])
    qe.add_lead("gmail.test", emails=[("gmailroofer@gmail.com", "freemail")])
    qe.add_lead("low.test")
    qe.judge.by_domain["low.test"] = {"family": ("not_met", "family-owned roofer"),
                                     "residential": ("not_met", "homeowners")}
    qe.add_lead("supplier-only.test", emails=[("rep@supplier.test", "other_domain")])
    qe.hunter.by_domain["supplier-only.test"] = hunter_fixture("generic")
    qe.run()
    assert qe.hunter.calls["domain_search"] == 1  # supplier-only: other_domain doesn't count as on-site
    assert qe.q("low.test")["drop_reason"] == "below_threshold"
    assert qe.q("supplier-only.test")["email"] == "office@gen.test"
    assert qe.q("supplier-only.test")["email_source_kind"] == "hunter"


def test_executive_owner_found_by_hunter(qe: QualifyEnv) -> None:
    qe.add_lead("keystone.test", people=[OWNER], company="Keystone Roofing, Inc")
    qe.hunter.by_domain["keystone.test"] = hunter_fixture("executive")
    qe.run()
    q = qe.q("keystone.test")
    assert (q["email"], q["email_type"], q["greeting_name"], q["qualified"]) == (
        "john@keystone.test", "owner", "John", 1)
    assert q["email_source_url"] == "https://www.bbb.org/profile/x"


def test_cap_hit_with_more_leads_queued(tmp_path: Path) -> None:
    with qualify_env(tmp_path, max_hunter_calls_per_run=1) as qe:
        for d in ("one.test", "two.test", "three.test"):
            qe.add_lead(d)
        qe.run()
        assert qe.hunter.calls["domain_search"] == 1
        assert [qe.q(d)["drop_reason"] for d in ("one.test", "two.test", "three.test")] == [
            "no_email", "hunter_cap", "hunter_cap"]  # rows are still written for every lead
        assert all(qe.q(d)["qualified"] == 0 for d in ("two.test", "three.test"))


def test_quota_response_maps_to_hunter_quota_and_stops_calling(qe: QualifyEnv) -> None:
    qe.add_lead("one.test")
    qe.add_lead("two.test")
    qe.hunter.by_domain["one.test"] = "quota"
    qe.run()
    assert qe.q("one.test")["drop_reason"] == "hunter_quota" and qe.q("two.test")["drop_reason"] == "hunter_quota"
    assert qe.hunter.calls["domain_search"] == 1  # 429 is never retried, and the pass stops calling


def test_no_credits_left_means_no_call(qe: QualifyEnv) -> None:
    qe.hunter.credits = 0
    qe.add_lead("one.test")
    qe.run()
    assert qe.q("one.test")["drop_reason"] == "hunter_quota"
    assert qe.hunter.calls["domain_search"] == 0 and qe.hunter.calls["account"] == 1


def test_source_filter(qe: QualifyEnv) -> None:
    qe.add_lead("nosrc.test")
    qe.hunter.by_domain["nosrc.test"] = hunter_fixture("unsourced")
    qe.run()
    assert qe.deps.runs_db.email_observations("R1", "nosrc.test") == []
    assert qe.q("nosrc.test")["drop_reason"] == "no_email"


def test_cache_means_no_second_call(qe: QualifyEnv) -> None:
    qe.add_lead("keystone.test", people=[OWNER])
    qe.hunter.by_domain["keystone.test"] = hunter_fixture("executive")
    qe.run()
    qe.deps.runs_db.delete_qualifications("R1", ["keystone.test"])
    qe.run()
    assert qe.hunter.calls["domain_search"] == 1 and qe.q("keystone.test")["email"] == "john@keystone.test"


def test_without_a_hunter_client_form_only_leads_are_no_email(qe: QualifyEnv) -> None:
    qe.deps.hunter = None
    qe.add_lead("form.test")
    qe.run()
    assert qe.q("form.test")["drop_reason"] == "no_email"
