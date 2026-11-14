"""The routers (design item 26; Supervisor contract: Code router rule order, Retries)."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import ValidationError

from outreach.deps import Deps
from outreach.graph.routers import ClaudeRouter, CodeRouter, decision_schema
from outreach.graph.supervisor import legal_moves, valid_decision
from outreach.models import Move, QuestionKey
from tests.fakes import FakeChat, Step, invalid, ok, refusal
from tests.test_legal_moves import ASKABLE, EXHAUSTED, st

M, K = Move, QuestionKey


@pytest.mark.parametrize(("status", "move"), [
    (st(unqualified_candidates=2, undrafted_qualified=1), M.qualify),  # 1. qualify when legal
    (st(undrafted_qualified=1, qualified_count=1), M.research),  # 2. research at row 4, even with draft legal
    (st(**EXHAUSTED, undrafted_qualified=1), M.draft),  # 3. draft
    (st(**EXHAUSTED), M.finish),  # 4. finish
    (st(**ASKABLE), M.finish),  # never ask, even when ask is legal
])
def test_code_router_order(status: Any, move: Move) -> None:
    assert CodeRouter().decide(status, legal_moves(status)).move == move


def claude(deps: Deps, *steps: Step) -> tuple[ClaudeRouter, FakeChat]:
    chat = FakeChat(list(steps), model="claude-opus-5-5", call_site="router")
    deps.llms["router"] = chat
    return ClaudeRouter(deps), chat


def test_claude_picks_a_legal_move(deps_fake: Deps) -> None:
    status = st(undrafted_qualified=1, qualified_count=1)
    router, chat = claude(deps_fake, ok({"move": "draft", "reason": "one lead is ready", "assumption": None}))
    d = router.decide(status, legal_moves(status), run_id="R1")
    assert d is not None and d.move == M.draft and valid_decision(d, status, legal_moves(status))
    assert chat.call_count == 1
    [call] = deps_fake.runs_db.api_calls("R1", call_site="router")
    assert call["model"] == "claude-opus-5-5"


def test_schema_only_admits_legal_moves_and_available_keys() -> None:
    schema = decision_schema(frozenset({M.ask, M.finish}), frozenset({K.extra_round}))
    assert schema.model_validate({"move": "ask", "reason": "r", "question_key": "extra_round"})
    with pytest.raises(ValidationError):
        schema.model_validate({"move": "research", "reason": "r"})
    with pytest.raises(ValidationError):
        schema.model_validate({"move": "ask", "reason": "r", "question_key": "change_city"})
    no_ask = decision_schema(frozenset({M.research, M.draft}), frozenset({K.extra_round}))
    with pytest.raises(ValidationError):
        no_ask.model_validate({"move": "draft", "reason": "r", "question_key": "extra_round"})
    props = no_ask.model_json_schema()["properties"]
    assert sorted(props["move"]["enum"]) == ["draft", "research"]


def test_illegal_move_twice_falls_back_after_two_calls(deps_fake: Deps) -> None:
    status = st(undrafted_qualified=1, qualified_count=1)
    illegal = json.dumps({"move": "finish", "reason": "done"})
    router, chat = claude(deps_fake, invalid(illegal), invalid(illegal))
    assert router.decide(status, legal_moves(status), run_id="R1") is None  # the supervisor falls back
    assert chat.call_count == 2  # the one validation retry, no more


def test_retry_can_recover(deps_fake: Deps) -> None:
    status = st(undrafted_qualified=1, qualified_count=1)
    router, chat = claude(deps_fake, invalid(json.dumps({"move": "finish", "reason": "x"})),
                          ok({"move": "research", "reason": "more leads needed"}))
    d = router.decide(status, legal_moves(status), run_id="R1")
    assert d is not None and d.move == M.research and chat.call_count == 2


def test_refusal_falls_back(deps_fake: Deps) -> None:
    status = st(undrafted_qualified=1, qualified_count=1)
    router, chat = claude(deps_fake, refusal())
    assert router.decide(status, legal_moves(status), run_id="R1") is None and chat.call_count == 1


def test_ask_with_no_key_is_invalid(deps_fake: Deps) -> None:
    status = st(**ASKABLE)
    router, _ = claude(deps_fake, ok({"move": "ask", "reason": "should I?"}))
    d = router.decide(status, legal_moves(status), run_id="R1")
    assert d is not None and d.move == M.ask and d.question_key is None
    assert not valid_decision(d, status, legal_moves(status))


def test_reason_is_cut_to_200_chars(deps_fake: Deps) -> None:
    status = st(undrafted_qualified=1, qualified_count=1)
    router, _ = claude(deps_fake, ok({"move": "draft", "reason": "x" * 500}))
    d = router.decide(status, legal_moves(status), run_id="R1")
    assert d is not None and len(d.reason) == 200
