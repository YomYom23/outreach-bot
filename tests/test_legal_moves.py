"""Phase 3 exit test: the truth table, and RunStatus built from runs.db."""

from __future__ import annotations

import itertools
import json
from typing import Any

import pytest

from outreach import __version__
from outreach.db.runs_db import RunsDB
from outreach.deps import Deps
from outreach.graph.supervisor import legal_moves
from outreach.models import Move, QuestionKey, ResearchStatus, RunStatus

K = QuestionKey


def st(**kw: Any) -> RunStatus:
    base: dict[str, Any] = dict(
        unqualified_candidates=0, qualified_count=0, near_miss_leads=0, target_qualified=4, research_rounds_used=0,
        max_research_rounds=3, last_round_new_companies=None, undrafted_qualified=0, questions_used=0,
        max_questions=3, decisions_used=0, max_decisions=20, answered_keys=frozenset(),
        available_question_keys=frozenset(), exhausted_reason=None, last_stage_stop_reason=None, router="claude")
    base.update(kw)
    return RunStatus(**base)


EXHAUSTED = dict(research_rounds_used=3, exhausted_reason="rounds")
ASKABLE = dict(**EXHAUSTED, available_question_keys=frozenset({K.extra_round, K.change_city}))
M = Move


@pytest.mark.parametrize(("row", "status", "expected"), [
    (1, st(unqualified_candidates=2, decisions_used=20, qualified_count=9), {M.qualify}),
    (2, st(decisions_used=20), {M.finish}),
    (2, st(decisions_used=20, undrafted_qualified=1), {M.draft}),
    (3, st(qualified_count=4), {M.finish}),
    (3, st(qualified_count=5, undrafted_qualified=2), {M.draft}),
    (4, st(), {M.research}),
    (4, st(undrafted_qualified=1, qualified_count=1), {M.research, M.draft}),
    (5, st(**ASKABLE), {M.ask, M.finish}),
    (5, st(**ASKABLE, undrafted_qualified=1), {M.ask, M.draft}),
    (6, st(**ASKABLE, router="code"), {M.finish}),
    (6, st(**ASKABLE, questions_used=3), {M.finish}),
    (6, st(**EXHAUSTED, undrafted_qualified=2), {M.draft}),
    (6, st(last_round_new_companies=0, exhausted_reason="empty_round"), {M.finish}),
])
def test_truth_table_rows(row: int, status: RunStatus, expected: set[Move]) -> None:
    assert legal_moves(status) == expected


def test_legal_set_is_never_empty_and_always_has_an_exit() -> None:
    grid = itertools.product([0, 2], [0, 4], [0, 1], [0, 20], [None, "rounds", "empty_round"], ["claude", "code"],
                             [frozenset(), frozenset({K.change_city})], [0, 3])
    for unq, qual, undrafted, dec, exhausted, router, keys, asked in grid:
        s = st(unqualified_candidates=unq, qualified_count=qual, undrafted_qualified=undrafted,
               decisions_used=dec, exhausted_reason=exhausted, router=router, available_question_keys=keys,
               questions_used=asked)
        legal = legal_moves(s)
        assert legal
        if not (unq or (exhausted is None and dec < 20 and qual < 4)):  # rows 2, 3, 5, 6
            assert legal & {M.draft, M.finish}


# --- RunStatus from runs.db -------------------------------------------------------------


@pytest.fixture
def run(deps_fake: Deps) -> RunsDB:
    db = deps_fake.runs_db
    db.create_run(run_id="R1", app_version=__version__, router="claude", prompt="p")
    db.write_plan("R1", version=1, status="frozen", city="Tucson, AZ", target_qualified=2,
                  checklist={"items": [{"id": f"i{n}", "text": f"t{n}", "check": "llm", "code_check": None}
                                       for n in (1, 2, 3)], "threshold": 2},
                  sender={}, checklist_key="k")
    return db


def research_round(db: RunsDB, leads: dict[str, ResearchStatus], plan_version: int = 1) -> int:
    r = db.open_round("R1", plan_version)
    db.queue_candidates("R1", r, list(leads))
    for lead_id, status in leads.items():
        db.set_candidate_status("R1", lead_id, status)
    counted = sum(s in (ResearchStatus.candidate, ResearchStatus.parked_no_email) for s in leads.values())
    db.close_round("R1", r, new_companies=counted, stop_reason="queue_empty")
    return r


def qualify(db: RunsDB, lead_id: str, points: int, email: str | None = "a@b.test", n: int = 2) -> None:
    db.write_qualification(run_id="R1", lead_id=lead_id, checklist_version=1, points=points,
                           verdicts_json=json.dumps([]), qualified=points >= n and email is not None, email=email)


def status(deps: Deps) -> RunStatus:
    return deps.runs_db.build_run_status("R1", deps.settings, deps.memory_db.is_blocked_or_contacted)


def test_start_state_is_research(run: RunsDB, deps_fake: Deps) -> None:
    s = status(deps_fake)
    assert (s.research_rounds_used, s.last_round_new_companies, s.exhausted_reason) == (0, None, None)
    assert legal_moves(s) == {M.research}


def test_unqualified_candidates_force_qualify(run: RunsDB, deps_fake: Deps) -> None:
    research_round(run, {"a.test": ResearchStatus.candidate, "b.test": ResearchStatus.parked_no_email,
                         "c.test": ResearchStatus.rejected_non_us})
    s = status(deps_fake)
    assert s.unqualified_candidates == 2
    assert legal_moves(s) == {M.qualify}


def test_all_filtered_round_counts_as_empty(run: RunsDB, deps_fake: Deps) -> None:
    research_round(run, {"dir.test": ResearchStatus.rejected_triage, "x.test": ResearchStatus.rejected_not_roofer})
    s = status(deps_fake)
    assert (s.last_round_new_companies, s.exhausted_reason) == (0, "empty_round")
    assert s.available_question_keys == {K.change_city}  # not extra_round: rounds remain
    assert legal_moves(s) == {M.ask, M.finish}


def test_seeds_run_reaches_round_2(run: RunsDB, deps_fake: Deps) -> None:
    research_round(run, {"seed-a.test": ResearchStatus.candidate})  # round 1 from seeds
    qualify(run, "seed-a.test", points=3)
    s = status(deps_fake)
    assert (s.qualified_count, s.research_rounds_used, s.exhausted_reason) == (1, 1, None)
    assert legal_moves(s) == {M.research, M.draft}


def test_decision_cap(run: RunsDB, deps_fake: Deps) -> None:
    for turn in range(deps_fake.settings.max_decisions):
        run.record_decision(run_id="R1", status_json=str(turn), legal_moves=["research", "draft"], router="claude",
                            decided_by="router" if turn % 2 else "fallback", chosen_move="research")
    run.record_decision(run_id="R1", status_json="forced", legal_moves=["qualify"], router="claude",
                        decided_by="forced", chosen_move="qualify")
    s = status(deps_fake)
    assert s.decisions_used == deps_fake.settings.max_decisions  # forced turns don't count
    assert legal_moves(s) == {M.finish}


def test_question_key_conditions(run: RunsDB, deps_fake: Deps) -> None:
    for n in range(3):
        research_round(run, {f"l{n}.test": ResearchStatus.candidate})
    qualify(run, "l0.test", points=1)  # near miss: N - 1
    qualify(run, "l1.test", points=0)
    qualify(run, "l2.test", points=3)
    s = status(deps_fake)
    assert (s.exhausted_reason, s.near_miss_leads, s.qualified_count) == ("rounds", 1, 1)
    assert s.available_question_keys == {K.extra_round, K.change_city, K.lower_threshold}

    run.record_answer(pause_id="q:R1:extra_round", run_id="R1", key="extra_round", value="no", origin="supervisor")
    s = status(deps_fake)
    assert s.available_question_keys == {K.change_city, K.lower_threshold}
    assert s.questions_used == 1

    run.add_override("R1", "extra_round", "1")  # rounds no longer used up
    s = status(deps_fake)
    assert (s.max_research_rounds, s.exhausted_reason, s.available_question_keys) == (4, None, frozenset())


def test_complete_run_offers_no_questions(run: RunsDB, deps_fake: Deps) -> None:
    for n in range(3):
        research_round(run, {f"l{n}.test": ResearchStatus.candidate})
        qualify(run, f"l{n}.test", points=3)
    s = status(deps_fake)
    assert s.complete and s.exhausted_reason == "rounds"
    assert s.available_question_keys == frozenset()


def test_new_plan_version_resets_last_round(run: RunsDB, deps_fake: Deps) -> None:
    research_round(run, {"x.test": ResearchStatus.rejected_triage})
    assert status(deps_fake).last_round_new_companies == 0
    plan = run.plan_model("R1")
    assert plan is not None
    run.write_plan_model(plan.model_copy(update={"version": 2, "city": "Mesa, AZ"}))
    assert status(deps_fake).last_round_new_companies is None


def test_undrafted_excludes_blocked_and_drafted(run: RunsDB, deps_fake: Deps) -> None:
    for lead_id in ("a.test", "b.test", "c.test"):
        research_round(run, {lead_id: ResearchStatus.candidate})
        qualify(run, lead_id, points=3)
    assert status(deps_fake).undrafted_qualified == 3
    deps_fake.memory_db.block_many(["a.test"], "R0")
    run.insert_draft(draft_id="d1", run_id="R1", lead_id="b.test", subject="s", body="b", claim_flags=[],
                     model=None, prompt_version=None, status="draft_failed")
    assert status(deps_fake).undrafted_qualified == 1


def test_status_json_is_stable() -> None:
    a = st(available_question_keys=frozenset({K.lower_threshold, K.change_city, K.extra_round}))
    assert '"available_question_keys":["change_city","extra_round","lower_threshold"]' in a.model_dump_json()
