"""Checklist scoring (design item 40; Decisions: Checklist scoring; Rules: no_ai_visible, Quote check)."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.qualify_support import QualifyEnv, qualify_env


@pytest.fixture
def qe(tmp_path: Path) -> Iterator[QualifyEnv]:
    with qualify_env(tmp_path) as env:
        yield env


def verdicts(qe: QualifyEnv, domain: str) -> dict[str, str]:
    return {v["item_id"]: v["value"] for v in json.loads(qe.q(domain)["verdicts_json"])}


def test_points_against_n(qe: QualifyEnv) -> None:
    qe.add_lead("all-met.test", emails=[("info@all-met.test", "same_domain")])
    qe.add_lead("one-met.test", emails=[("info@one-met.test", "same_domain")])
    qe.judge.by_domain["one-met.test"] = {"family": ("not_met", "family-owned roofer")}
    qe.add_lead("widget.test", emails=[("info@widget.test", "same_domain")], markers=["Tidio"])
    qe.set_checklist(3)
    qe.run()
    assert (qe.q("all-met.test")["points"], qe.q("all-met.test")["qualified"]) == (3, 1)
    assert (qe.q("one-met.test")["points"], qe.q("one-met.test")["drop_reason"]) == (2, "below_threshold")
    assert verdicts(qe, "widget.test")["no_ai"] == "not_met"
    assert qe.q("one-met.test")["email"] is None  # no email is chosen below N


def test_unknown_scores_zero_and_missing_items_are_unknown(qe: QualifyEnv) -> None:
    qe.add_lead("vague.test", emails=[("info@vague.test", "same_domain")])
    qe.judge.by_domain["vague.test"] = {"family": ("unknown", None)}
    qe.run()
    assert verdicts(qe, "vague.test") == {"no_ai": "met", "family": "unknown", "residential": "met"}
    assert qe.q("vague.test")["points"] == 2


def test_quote_not_on_page_becomes_unknown(qe: QualifyEnv) -> None:
    qe.add_lead("liar.test", emails=[("info@liar.test", "same_domain")])
    qe.judge.by_domain["liar.test"] = {"family": ("met", "Owned by the Smith family since 1950"),
                                      "residential": ("not_met", "We only do commercial work")}
    qe.run()
    v = verdicts(qe, "liar.test")
    assert (v["family"], v["residential"]) == ("unknown", "unknown")
    assert qe.q("liar.test")["points"] == 1


def test_no_ai_visible_clause_order(qe: QualifyEnv) -> None:
    qe.add_lead("thin.test", emails=[("info@thin.test", "same_domain")], ok_pages=1, markers=["Tidio"])
    qe.add_lead("chat.test", emails=[("info@chat.test", "same_domain")], markers=["Intercom"])
    qe.add_lead("quiet.test", emails=[("info@quiet.test", "same_domain")])
    qe.run()
    assert verdicts(qe, "thin.test")["no_ai"] == "unknown"  # 1. too few ok pages, even with a marker
    assert verdicts(qe, "chat.test")["no_ai"] == "not_met"  # 2. a marker
    assert verdicts(qe, "quiet.test")["no_ai"] == "met"  # 3. otherwise


def test_one_checklist_call_per_lead_and_cached_on_requalify(qe: QualifyEnv) -> None:
    qe.add_lead("a.test", emails=[("info@a.test", "same_domain")])
    qe.add_lead("b.test", emails=[("info@b.test", "same_domain")])
    qe.run()
    assert qe.checklist_calls() == 2
    qe.deps.runs_db.delete_qualifications("R1", ["a.test", "b.test"])
    qe.run()
    assert qe.checklist_calls() == 2 and qe.q("a.test")["qualified"] == 1


def test_rescore_needs_no_model_call_and_returns_leads_to_requalify(qe: QualifyEnv) -> None:
    qe.add_lead("near.test", emails=[("info@near.test", "same_domain")])
    qe.judge.by_domain["near.test"] = {"family": ("not_met", "family-owned roofer")}
    qe.add_lead("form-only.test")
    qe.judge.by_domain["form-only.test"] = {"family": ("not_met", "family-owned roofer")}
    qe.set_checklist(3)
    qe.run()
    calls = qe.checklist_calls()
    assert qe.q("near.test")["drop_reason"] == "below_threshold"
    back = qe.deps.runs_db.rescore("R1", 2, 3)
    assert back == ["near.test", "form-only.test"] or set(back) == {"near.test", "form-only.test"}
    assert qe.checklist_calls() == calls
    qe.set_checklist(2, version=3)
    qe.run()  # the returned leads go back through qualify: verdicts from the cache, then email choice
    assert qe.checklist_calls() == calls
    assert qe.q("near.test")["qualified"] == 1 and qe.q("near.test")["email"] == "info@near.test"
    assert qe.q("form-only.test")["drop_reason"] == "no_email"
