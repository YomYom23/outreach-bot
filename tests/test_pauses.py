"""Phase 3 exit test: the pause contract (pause ids, mismatches, plan edits, remembered answers, questions)."""

from __future__ import annotations

from typing import Any

import pytest

from outreach.db.memory_db import MemoryDB
from outreach.db.runs_db import RunsDB
from outreach.deps import Deps
from outreach.graph.routers import CodeRouter
from outreach.models import PlanAnswer, QuestionAnswer
from tests.conftest import make_settings
from tests.helpers import INTAKE, THREE_ROUNDS, AskingRouter, Driver, make_graph


def reopen(deps: Deps) -> Deps:
    """The same files, new connections: what a new process sees after the terminal closed."""
    s = deps.settings
    return Deps(settings=s, profile=deps.profile, runs_db=RunsDB(s.runs_db_path, now=deps.clock.now),
                memory_db=MemoryDB(s.memory_db_path, now=deps.clock.now), llms=deps.llms, mailer=deps.mailer,
                clock=deps.clock, ids=deps.ids)


def test_resume_same_pause_id(graph_fake: Any, deps_fake: Deps) -> None:
    _graph, _stages, d = graph_fake()
    d.create_run()
    first = d.start()
    assert first is not None and first.pause_id == "q:R1:sender_name"

    deps2 = reopen(deps_fake)
    graph2, _ = make_graph(deps2)
    again = Driver(graph2, deps2).resume_none()
    assert again == first
    assert deps2.llms["parse"].calls == []  # parse_request isn't repeated


def test_mismatched_answer_reinterrupts_with_same_payload(graph_fake: Any) -> None:
    _g, _s, d = graph_fake()
    d.create_run()
    p = d.start()
    assert p is not None
    stale = d.answer(QuestionAnswer(pause_id="q:R1:city", key="sender_name", value="Sam"))
    assert stale == p and "error" not in stale.context
    wrong_kind = d.answer(PlanAnswer(pause_id=p.pause_id, action="confirm"))
    assert wrong_kind == p
    nxt = d.answer(QuestionAnswer(pause_id=p.pause_id, key="sender_name", value="Sam Lee"))
    assert nxt is not None and nxt.pause_id == "q:R1:business_name"


def test_invalid_answer_shows_error(graph_fake: Any, deps_fake: Deps) -> None:
    _g, _s, d = graph_fake()
    d.create_run()
    p = d.start()
    assert p is not None
    p2 = d.answer(QuestionAnswer(pause_id=p.pause_id, key="sender_name", value="   "))
    assert p2 is not None and p2.pause_id == p.pause_id and p2.context["error"]
    assert deps_fake.runs_db.answers("R1") == []


def test_intake_asks_city_when_prompt_has_none(deps_fake: Deps) -> None:
    from tests.helpers import parse_chat

    graph, _ = make_graph(deps_fake, parse=parse_chat(city=None))
    d = Driver(graph, deps_fake)
    d.create_run()
    p = d.start()
    assert p is not None and p.context["key"] == "city" and "Tucson, AZ" in p.context["suggestions"]
    p = d.question(p, "Mesa, AZ")
    assert p is not None and p.context["key"] == "sender_name"
    assert deps_fake.runs_db.plan_model("R1").city == "Mesa, AZ"  # type: ignore[union-attr]


def to_plan(d: Driver) -> Any:
    p = d.start()
    while p is not None and p.kind == "question":
        p = d.question(p, INTAKE[p.context["key"]])
    assert p is not None and p.kind == "plan"
    return p


def test_plan_edit(graph_fake: Any, deps_fake: Deps) -> None:
    _g, _s, d = graph_fake()
    d.create_run()
    p = to_plan(d)
    db = deps_fake.runs_db
    plan = p.context["plan"]
    assert plan["city"] == "Tucson, AZ" and plan["checklist"]["threshold"] is None
    assert [i["check"] for i in plan["checklist"]["items"]] == ["code", "llm", "llm"]  # prompt item + 2 defaults

    def edit(edits: dict[str, str]) -> Any:
        nonlocal p
        p = d.answer(PlanAnswer(pause_id=p.pause_id, action="edit", edits=edits))
        return p

    start = int(p.pause_id.rsplit(":", 1)[1])  # each intake answer wrote a plan version
    edit({"city": "Mesa, AZ", "target": "6", "sender.tone": "warm"})
    assert p.pause_id == f"plan:R1:{start + 1}" and p.context["plan"]["target_qualified"] == 6
    edit({"item.add": "No chatbot anywhere on the site"})
    added = p.context["plan"]["checklist"]["items"][-1]
    assert (added["id"], added["check"], added["code_check"]) == ("i4", "code", "no_ai_visible")
    edit({"item.i2.text": "Family owned", "item.i3.remove": "1"})
    assert [i["id"] for i in p.context["plan"]["checklist"]["items"]] == ["i1", "i2", "i4"]
    assert p.context["plan"]["checklist"]["items"][1]["text"] == "Family owned"

    version = p.pause_id
    for bad in ({"bogus": "x"}, {"target": "lots"}, {"item.i9.text": "x"}, {"threshold": "0"}):
        edit(bad)
        assert p.pause_id == version and p.context["error"]
    p = d.answer(PlanAnswer(pause_id=p.pause_id, action="confirm"))
    assert p is not None and "threshold" in p.context["error"]
    assert db.plan_model("R1").status == "draft"  # type: ignore[union-attr]

    edit({"threshold": "2"})
    nxt = d.answer(PlanAnswer(pause_id=p.pause_id, action="confirm", remember={"checklist"}))
    frozen = db.plan_model("R1")
    assert frozen is not None and frozen.status == "frozen" and frozen.checklist.key
    assert (frozen.city, frozen.target_qualified, frozen.sender.tone) == ("Mesa, AZ", 6, "warm")
    assert deps_fake.memory_db.latest_checklist()["threshold"] == 2  # type: ignore[index]
    assert nxt is not None and nxt.kind == "draft_review"


def test_resume_at_plan(graph_fake: Any, deps_fake: Deps) -> None:
    _g, _s, d = graph_fake()
    d.create_run()
    p = to_plan(d)
    deps2 = reopen(deps_fake)
    graph2, _ = make_graph(deps2)
    d2 = Driver(graph2, deps2)
    assert d2.resume_none() == p
    p2 = d2.answer(PlanAnswer(pause_id=p.pause_id, action="edit", edits={"threshold": "2"}))
    assert p2 is not None and p2.pause_id == "plan:R1:5"  # 1 + three intake answers + this edit


def test_remembered_intake_answers_not_asked(graph_fake: Any, deps_fake: Deps) -> None:
    graph, _s, d = graph_fake()
    d.create_run()
    d.through_intake(d.start(), remember=("sender", "checklist"))
    d2 = Driver(graph, deps_fake, run_id="R2")
    d2.create_run()
    p = d2.start()
    assert p is not None and p.kind == "plan"  # no sender questions, no threshold to set
    assert p.context["plan"]["sender"]["name"] == INTAKE["sender_name"]
    assert p.context["threshold_valid"]
    assert [a["key"] for a in deps_fake.runs_db.answers("R2")] == []


def test_remembered_supervisor_answer_applied_not_asked(deps_fake: Deps) -> None:
    deps_fake.memory_db.remember_preference("extra_round", "yes")
    graph, stages = make_graph(deps_fake, THREE_ROUNDS)
    d = Driver(graph, deps_fake)
    d.create_run(router="code")  # remembered answers apply under either router
    p = d.through_intake(d.start())
    while p is not None:
        assert p.kind == "draft_review", "a remembered key must not be asked"
        p = d.review(p, "approve")
    assert stages.calls["research"] == 4
    answers = [(a["key"], a["value"], a["origin"]) for a in deps_fake.runs_db.answers("R1")]
    assert ("extra_round", "yes", "memory") in answers
    assumptions = [r["assumption"] for r in deps_fake.runs_db.decisions("R1") if r["assumption"]]
    assert "applied remembered answer for extra_round" in assumptions
    status = deps_fake.runs_db.build_run_status("R1", deps_fake.settings)
    assert status.questions_used == 0  # memory answers don't count


def test_supervisor_question_extra_round_and_cap(deps_fake: Deps, tmp_path: Any) -> None:
    deps_fake.settings = make_settings(tmp_path, max_questions=1)
    router = AskingRouter()
    graph, stages = make_graph(deps_fake, THREE_ROUNDS, routers={"claude": router, "code": CodeRouter()})
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    p = d.through_intake(d.start())
    assert p is not None and p.pause_id == "q:R1:extra_round" and p.allowed_answers == ["yes", "no"]
    bad = d.question(p, "maybe")
    assert bad is not None and bad.context["error"]
    p = d.question(p, "yes", remember=True)
    assert stages.calls["research"] == 4
    while p is not None:
        assert p.kind == "draft_review", "only one question allowed (max_questions=1)"
        p = d.review(p, "approve")
    assert deps_fake.memory_db.preference("extra_round") == "yes"
    asked = [r for r in deps_fake.runs_db.decisions("R1") if r["chosen_move"] == "ask"]
    assert len(asked) == 1 and asked[0]["decided_by"] == "router" and asked[0]["shadow_move"] != "ask"


def test_change_city_answer_writes_plan_version(deps_fake: Deps) -> None:
    graph, stages = make_graph(deps_fake, routers={"claude": AskingRouter(prefer=("change_city",)),
                                                   "code": CodeRouter()})
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    p = d.through_intake(d.start())
    assert p is not None and p.context["key"] == "change_city"
    d.question(p, "Mesa, AZ")
    plan = deps_fake.runs_db.plan_model("R1")
    assert plan is not None and plan.city == "Mesa, AZ" and plan.status == "frozen" and plan.version == 6
    assert deps_fake.runs_db.overrides("R1", "extra_round") == ["1"]
    assert stages.calls["research"] == 4


def test_lower_threshold_answer_rescores(deps_fake: Deps) -> None:
    graph, stages = make_graph(deps_fake, routers={"claude": AskingRouter(prefer=("lower_threshold",)),
                                                   "code": CodeRouter()})
    d = Driver(graph, deps_fake)
    d.create_run(router="claude")
    p = d.through_intake(d.start())
    assert p is not None and p.context["key"] == "lower_threshold" and p.allowed_answers == ["1", "no"]
    calls_before = stages.calls["qualify"]
    p = d.question(p, "1")
    db = deps_fake.runs_db
    assert db.qualification("R1", "copper-top.test")["qualified"] == 1  # type: ignore[index]
    assert stages.calls["qualify"] == calls_before + 1  # eagle-roofing (no email) went back through qualify
    assert db.build_run_status("R1", deps_fake.settings).qualified_count == 4
    assert p is not None and p.kind == "draft_review"


@pytest.mark.parametrize("router", ["code", "claude"])
def test_code_router_never_asks(deps_fake: Deps, router: str) -> None:
    graph, _ = make_graph(deps_fake)  # the scripted Claude router never picks `ask` unless told to
    d = Driver(graph, deps_fake)
    d.create_run(router=router)
    p = d.through_intake(d.start())
    while p is not None:
        assert p.kind == "draft_review"
        p = d.review(p, "approve")
    decisions = deps_fake.runs_db.decisions("R1")
    assert all(r["chosen_move"] != "ask" for r in decisions)
    if router == "claude":
        assert {r["decided_by"] for r in decisions} == {"forced", "router"}
