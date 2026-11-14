"""Drives the parent graph the way the CLI does, without a terminal."""

from __future__ import annotations

import re
from typing import Any

from langgraph.types import Command
from pydantic import BaseModel

from outreach import __version__
from outreach.models import PausePayload, PlanAnswer, QuestionAnswer, ReviewAnswer
from outreach.tracing import run_config
from tests.canned_stages import CannedLead, CannedStages, lead
from tests.fakes import FakeChat

PROMPT = "find me leads in Tucson that don't use AI"
INTAKE = {"sender_name": "Sam Lee", "business_name": "Lee Automation", "call_to_action": "reply for a 15-minute call"}

DEFAULT_ROUNDS: list[list[CannedLead]] = [
    [lead("acme-roofing.test"), lead("best-roof.test"), lead("copper-top.test", points=1)],
    [lead("desert-roofs.test"), lead("eagle-roofing.test", email=None)],
]
# Round 3 finds a company, so the rounds are used up without the last one being empty.
THREE_ROUNDS: list[list[CannedLead]] = [*DEFAULT_ROUNDS, [lead("fox-roofing.test", points=0)]]


def parse_chat(city: str | None = "Tucson, AZ", items: list[dict] | None = None) -> FakeChat:
    reply = {"city": city, "target": None,
             "items": items if items is not None else [{"text": "Doesn't use AI on the website",
                                                        "code_check": "no_ai_visible"}]}
    return FakeChat(call_site="parse", responder=lambda _m: reply)


def drafter_reply(messages: list[Any]) -> dict:
    text = messages[0].content
    m = re.search(r"Company: (.+?) \(([^)]+)\)", text)
    company, lead_id = (m.group(1), m.group(2)) if m else ("there", "?")
    return {"subject": f"Quick idea for {company}",
            "body": f"Hi there,\n\nI saw {company} ({lead_id}) online. Could we talk?"}


def drafter_chat() -> FakeChat:
    return FakeChat(model="claude-opus-5-5", call_site="drafter", responder=drafter_reply)


class Driver:
    """start() / answer() return the next PausePayload, or None when the graph has finished."""

    def __init__(self, graph: Any, deps: Any, run_id: str = "R1") -> None:
        self.graph, self.deps, self.run_id = graph, deps, run_id
        self.history: list[PausePayload] = []

    @property
    def config(self) -> dict:
        status = self.deps.runs_db.build_run_status(self.run_id, self.deps.settings)
        return run_config(self.run_id, status.router, self.deps.settings, status)

    def create_run(self, *, router: str = "code", prompt: str = PROMPT, seeds: list[str] | None = None,
                   city: str | None = None, app_version: str = __version__) -> None:
        self.deps.runs_db.create_run(run_id=self.run_id, app_version=app_version, router=router, prompt=prompt,
                                     seeds=seeds, city_override=city)

    def _stream(self, inp: Any) -> PausePayload | None:
        for _ in self.graph.stream(inp, self.config, durability="sync", stream_mode=["custom", "updates"],
                                   subgraphs=True):
            pass
        return self.pending()

    def pending(self) -> PausePayload | None:
        state = self.graph.get_state(self.config)
        interrupts = [i for t in state.tasks for i in t.interrupts]
        if not interrupts:
            return None
        payload = PausePayload.model_validate(interrupts[0].value)
        self.history.append(payload)
        return payload

    def start(self) -> PausePayload | None:
        return self._stream({"run_id": self.run_id})

    def resume_none(self) -> PausePayload | None:
        return self._stream(None)

    def answer(self, answer: BaseModel | dict) -> PausePayload | None:
        value = answer.model_dump(mode="json") if isinstance(answer, BaseModel) else answer
        return self._stream(Command(resume=value))

    # --- shortcuts ---------------------------------------------------------

    def through_intake(self, p: PausePayload | None, *, threshold: int | None = 2,
                       remember: tuple[str, ...] = (), answers: dict[str, str] | None = None) -> PausePayload | None:
        """Answers intake questions from INTAKE, sets N, confirms the plan. Returns the next pause."""
        answers = {**INTAKE, **(answers or {})}
        while p is not None and p.kind == "question" and p.context.get("origin") == "intake":
            p = self.answer(QuestionAnswer(pause_id=p.pause_id, key=p.context["key"],
                                           value=answers[p.context["key"]]))
        assert p is not None and p.kind == "plan", p
        if threshold is not None:
            p = self.answer(PlanAnswer(pause_id=p.pause_id, action="edit", edits={"threshold": str(threshold)}))
            assert p is not None and p.kind == "plan"
        return self.answer(PlanAnswer(pause_id=p.pause_id, action="confirm", remember=set(remember)))  # type: ignore[arg-type]

    def review(self, p: PausePayload, action: str, **kw: Any) -> PausePayload | None:
        assert p.kind == "draft_review", p
        return self.answer(ReviewAnswer(pause_id=p.pause_id, draft_id=p.context["draft_id"],
                                        shown_hash=p.context["shown_hash"], action=action, **kw))  # type: ignore[arg-type]

    def question(self, p: PausePayload, value: str, remember: bool = False) -> PausePayload | None:
        assert p.kind == "question", p
        return self.answer(QuestionAnswer(pause_id=p.pause_id, key=p.context["key"], value=value,
                                          remember=remember))


class AskingRouter:
    """A scripted stand-in for the Claude router: asks whenever `ask` is legal, preferring `prefer` order."""

    def __init__(self, prefer: tuple[str, ...] = ("extra_round", "change_city", "lower_threshold")) -> None:
        self.prefer = prefer
        self.calls = 0

    def decide(self, status: Any, legal: Any, **_: Any) -> Any:
        from outreach.graph.routers import CodeRouter
        from outreach.models import Decision, Move, QuestionKey

        self.calls += 1
        if Move.ask in legal:
            key = next((k for k in self.prefer if QuestionKey(k) in status.available_question_keys), None)
            if key is not None:
                return Decision(move=Move.ask, reason="the user should decide", question_key=QuestionKey(key))
        return CodeRouter().decide(status, legal)


def router_chat(prefer_ask: tuple[str, ...] = ()) -> FakeChat:
    """A scripted Claude router: reads the legal moves and keys from the prompt, asks for the first key in
    `prefer_ask` when it may, and otherwise follows the code router's order."""
    import re

    def reply(messages: list[Any]) -> dict:
        text = messages[-1].content
        legal = re.search(r"^Legal moves: (.*)$", text, re.MULTILINE).group(1).split(", ")  # type: ignore[union-attr]
        keys = re.search(r"^Question keys you may use: (.*)$", text, re.MULTILINE).group(1)  # type: ignore[union-attr]
        key = next((k for k in prefer_ask if k in keys.split(", ")), None)
        if "ask" in legal and key:
            return {"move": "ask", "reason": "only the user can decide this", "question_key": key}
        move = next(m for m in ("qualify", "research", "draft", "finish") if m in legal)
        return {"move": move, "reason": f"scripted: {move}", "assumption": None}

    return FakeChat(model="claude-opus-5-5", call_site="router", responder=reply)


def make_graph(deps: Any, rounds: list[list[CannedLead]] | None = None, *, test_hooks: bool = False,
               parse: FakeChat | None = None, drafter: FakeChat | None = None,
               routers: dict | None = None, router: FakeChat | None = None) -> tuple[Any, CannedStages]:
    from outreach.graph.build import build_graph

    deps.llms = {"parse": parse or parse_chat(), "drafter": drafter or drafter_chat(),
                 "router": router or router_chat()}
    stages = CannedStages(deps, rounds if rounds is not None else DEFAULT_ROUNDS)
    return build_graph(deps, stages=stages.nodes(), test_hooks=test_hooks, routers=routers), stages
