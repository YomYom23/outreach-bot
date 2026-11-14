"""Routers pick a move when more than one is legal. The supervisor validates whatever they return."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, Protocol

from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, create_model

from outreach.llm import LLMContext, load_prompt, structured_call
from outreach.models import Decision, Move, QuestionKey, RunStatus

if TYPE_CHECKING:
    from outreach.deps import Deps


REASON_CHARS = 200


class Router(Protocol):
    def decide(self, status: RunStatus, legal: frozenset[Move], *, run_id: str | None = None,
               config: dict | None = None) -> Decision | None:
        """Returns a Decision, or None when it failed (the supervisor then uses the code router)."""
        ...


class CodeRouter:
    """The fixed rule order from the Supervisor contract. Never asks."""

    name = "code"

    def decide(self, status: RunStatus, legal: frozenset[Move], *, run_id: str | None = None,
               config: dict | None = None) -> Decision:
        if Move.qualify in legal:
            return Decision(move=Move.qualify, reason="companies are waiting to be qualified")
        if Move.research in legal:
            return Decision(move=Move.research,
                            reason=f"{status.qualified_count} of {status.target_qualified} qualified; "
                                   "research is not exhausted")
        if Move.draft in legal:
            return Decision(move=Move.draft, reason=f"{status.undrafted_qualified} qualified leads have no draft")
        return Decision(move=Move.finish, reason="nothing left to do")


def decision_schema(legal: frozenset[Move], ask_keys: frozenset[QuestionKey]) -> type[BaseModel]:
    """A per-turn output model: `move` can only be a legal move, `question_key` only an available key."""
    moves = tuple(sorted(m.value for m in legal))
    keys = tuple(sorted(k.value for k in ask_keys)) if Move.ask in legal else ()
    # Literals built at runtime from this turn's legal set; typed Any because they aren't static types.
    move_type: Any = Literal[moves]
    key_type: Any = Literal[keys] | None if keys else None
    return create_model(
        "RouterDecision",
        move=(move_type, Field(description="One of the legal moves.")),
        reason=(str, Field(description="One sentence: why this move now.")),
        question_key=(key_type, Field(default=None, description="Only for `ask`: which question.")),
        assumption=(str | None, Field(default=None, description="What you assumed instead of asking, or null.")),
    )


def router_prompt(status: RunStatus, legal: frozenset[Move]) -> str:
    """Counts and enums only: RunStatus holds no page or model text (test_router_prompt)."""
    template, _version = load_prompt("router")
    keys = sorted(k.value for k in status.available_question_keys) if Move.ask in legal else []
    return template.format(questions_left=max(status.max_questions - status.questions_used, 0),
                           legal_moves=", ".join(sorted(m.value for m in legal)),
                           question_keys=", ".join(keys) or "(none: `ask` is not legal)",
                           status=status.model_dump_json(indent=1, exclude={"router"}))


class ClaudeRouter:
    """Opus picks among the legal moves. Validation happens twice: the schema only admits legal moves and
    available keys, and the supervisor's `valid_decision` checks again. Any failure returns None, so the code
    router decides that turn (`decided_by=fallback`). At most 2 model calls: structured_call's one retry."""

    name = "claude"

    def __init__(self, deps: Deps) -> None:
        self.deps = deps

    def decide(self, status: RunStatus, legal: frozenset[Move], *, run_id: str | None = None,
               config: dict | None = None) -> Decision | None:
        schema = decision_schema(legal, status.available_question_keys)
        _template, version = load_prompt("router")
        ctx = LLMContext(runs_db=self.deps.runs_db, settings=self.deps.settings, run_id=run_id,
                         prompt_version=version, ids=self.deps.ids, config=config)
        out = structured_call(self.deps.llms["router"], schema,
                              [HumanMessage(content=router_prompt(status, legal))], "router", ctx=ctx)
        if out is None:
            return None
        data = out.model_dump()
        key = data.get("question_key")
        return Decision(move=Move(data["move"]), reason=str(data["reason"])[:REASON_CHARS],
                        question_key=QuestionKey(key) if key else None, assumption=data.get("assumption"))


def default_routers(deps: Deps) -> dict[str, Router]:
    return {"code": CodeRouter(), "claude": ClaudeRouter(deps)}
