"""The Claude router inside whole runs (G2, CUJ 4.2), with a scripted router model."""

from __future__ import annotations

from pathlib import Path

from outreach.deps import Deps
from tests.conftest import make_settings
from tests.fakes import FakeChat, refusal
from tests.helpers import THREE_ROUNDS, Driver, make_graph, router_chat


def finish_reviews(d: Driver, p):  # type: ignore[no-untyped-def]
    while p is not None and p.kind == "draft_review":
        p = d.review(p, "approve")
    return p


def test_router_called_only_when_more_than_one_move_is_legal(deps_fake: Deps) -> None:
    """G2: forced turns make no model call; every router call is recorded as a router or fallback turn."""
    chat = router_chat()
    graph, _ = make_graph(deps_fake, router=chat)
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    assert finish_reviews(d, d.through_intake(d.start())) is None
    decisions = deps_fake.runs_db.decisions("R1")
    multi = [r for r in decisions if len(r["legal_moves"].split(",")) > 1]
    assert multi, "the fixture run should have at least one real choice"
    assert all(r["decided_by"] == "router" for r in multi)
    assert all(r["decided_by"] == "forced" for r in decisions if r not in multi)
    assert chat.call_count == len(multi)
    assert len(deps_fake.runs_db.api_calls("R1", call_site="router")) == chat.call_count


def test_a_refusing_router_never_stalls_the_run(deps_fake: Deps) -> None:
    """Every router turn refuses: the code router decides each one, and the run still finishes."""
    chat = FakeChat([refusal() for _ in range(50)], model="claude-opus-5-5", call_site="router")
    graph, _ = make_graph(deps_fake, router=chat)
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    assert finish_reviews(d, d.through_intake(d.start())) is None
    decisions = deps_fake.runs_db.decisions("R1")
    multi = [r for r in decisions if len(r["legal_moves"].split(",")) > 1]
    assert multi and all(r["decided_by"] == "fallback" and r["chosen_move"] == r["shadow_move"] for r in multi)
    assert chat.call_count == len(multi)  # refusals aren't retried: one call per turn


def test_cuj_4_2_extra_round(deps_fake: Deps, tmp_path: Path) -> None:
    deps_fake.settings = make_settings(tmp_path, max_questions=1)
    graph, stages = make_graph(deps_fake, THREE_ROUNDS, router=router_chat(prefer_ask=("extra_round",)))
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    p = d.through_intake(d.start())
    assert p is not None and p.context["key"] == "extra_round"
    p = d.question(p, "yes")
    assert stages.calls["research"] == 4  # the extra round ran
    assert finish_reviews(d, p) is None
    asked = [r for r in deps_fake.runs_db.decisions("R1") if r["chosen_move"] == "ask"]
    assert len(asked) == 1 and asked[0]["decided_by"] == "router" and asked[0]["question_key"] == "extra_round"


def test_cuj_4_2_change_city(deps_fake: Deps) -> None:
    graph, stages = make_graph(deps_fake, router=router_chat(prefer_ask=("change_city",)))
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    p = d.through_intake(d.start())
    assert p is not None and p.context["key"] == "change_city"
    d.question(p, "Mesa, AZ")
    plan = deps_fake.runs_db.plan_model("R1")
    assert plan is not None and plan.city == "Mesa, AZ"
    assert stages.calls["research"] == 4


def test_cuj_4_2_lower_threshold(deps_fake: Deps) -> None:
    graph, stages = make_graph(deps_fake, router=router_chat(prefer_ask=("lower_threshold",)))
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    p = d.through_intake(d.start())
    assert p is not None and p.context["key"] == "lower_threshold"
    before = stages.calls["qualify"]
    p = d.question(p, "1")
    assert stages.calls["qualify"] == before + 1
    assert deps_fake.runs_db.build_run_status("R1", deps_fake.settings).qualified_count == 4
    assert finish_reviews(d, p) is None
