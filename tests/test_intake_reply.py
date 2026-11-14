"""Intake replies that are questions get an answer, not stored as the value (manual check, 2026-11-13).

In the manual run, "what do you mean" became the sender name and signed all 8 emails.
"""

from __future__ import annotations

import re
from typing import Any

from outreach.deps import Deps
from tests.fakes import FakeChat, refusal
from tests.helpers import Driver, make_graph


def intake_reply_chat() -> FakeChat:
    def reply(messages: list[Any]) -> dict:
        text = re.search(r"<reply>(.*)</reply>", messages[-1].content, re.S).group(1)  # type: ignore[union-attr]
        if "what do you mean" in text.lower() or text.endswith("?"):
            return {"kind": "question", "reply": "It's the name your emails are signed with, e.g. 'Sam Lee'."}
        return {"kind": "answer", "reply": None}

    return FakeChat(model="claude-haiku-5-5", call_site="intake_reply", responder=reply)


def start(deps: Deps, chat: FakeChat) -> tuple[Driver, Any]:
    graph, _ = make_graph(deps)
    deps.llms["intake_reply"] = chat
    d = Driver(graph, deps)
    d.create_run()
    p = d.start()
    assert p is not None and p.context["key"] == "sender_name"
    return d, p


def test_a_question_back_gets_an_answer_and_the_same_question(deps_fake: Deps) -> None:
    chat = intake_reply_chat()
    d, p = start(deps_fake, chat)
    again = d.question(p, "what do you mean")
    assert again is not None and again.pause_id == p.pause_id
    assert "signed with" in again.context["explanation"] and "error" not in again.context
    nxt = d.question(again, "Sam Lee")
    assert nxt is not None and nxt.context["key"] == "business_name"
    [row] = [r for r in deps_fake.runs_db.conn.execute("SELECT key, value FROM answers") if r[0] == "sender_name"]
    assert row[1] == "Sam Lee"
    calls_so_far = chat.call_count
    d.question(nxt, "Lee Automation")
    assert chat.call_count == calls_so_far + 1  # the replays of earlier answers came from llm_cache


def test_a_failing_classifier_never_blocks_intake(deps_fake: Deps) -> None:
    d, p = start(deps_fake, FakeChat([refusal()], model="claude-haiku-5-5", call_site="intake_reply"))
    nxt = d.question(p, "Sam Lee")
    assert nxt is not None and nxt.context["key"] == "business_name"
