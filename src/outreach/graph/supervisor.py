"""The supervisor: truth table, remembered answers, router call with a shadow code router, decision log."""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from outreach.graph import Node
from outreach.graph.plan_edits import apply_answer
from outreach.graph.routers import CodeRouter, Router
from outreach.models import DecidedBy, Decision, Move, PendingQuestion, QuestionKey, RunPlan, RunStatus

if TYPE_CHECKING:
    from outreach.deps import Deps
    from outreach.state import ParentState

ROUTES: dict[Move, str] = {
    Move.research: "run_research",
    Move.qualify: "run_qualify",
    Move.ask: "ask_user",
    Move.draft: "drafter",
    Move.finish: "finalize",
}
REASON_CHARS = 200


def legal_moves(s: RunStatus) -> frozenset[Move]:
    """The truth table (design: Supervisor contract). Rows are checked top to bottom; never empty."""
    draft_or_finish = {Move.draft} if s.undrafted_qualified > 0 else {Move.finish}
    if s.unqualified_candidates > 0:  # 1
        return frozenset({Move.qualify})
    if s.decisions_used >= s.max_decisions:  # 2
        return frozenset(draft_or_finish)
    if s.complete:  # 3
        return frozenset(draft_or_finish)
    if not s.exhausted:  # 4
        return frozenset({Move.research} | ({Move.draft} if s.undrafted_qualified > 0 else set()))
    if s.ask_available:  # 5
        return frozenset({Move.ask} | draft_or_finish)
    return frozenset(draft_or_finish)  # 6


def question_for(key: QuestionKey, plan: RunPlan | None, deps: Deps) -> PendingQuestion:
    """The question text always comes from a fixed template, never from model output."""
    n = plan.checklist.threshold if plan and plan.checklist.threshold else 2
    text = deps.profile.question_templates[key.value].format(max_n=n - 1)
    allowed: list[str] | None = None
    if key == QuestionKey.extra_round:
        allowed = ["yes", "no"]
    elif key == QuestionKey.lower_threshold:
        allowed = [str(i) for i in range(1, n)] + ["no"]
    return PendingQuestion(key=key, origin="supervisor", prompt_text=text, allowed_answers=allowed)


def valid_decision(d: Decision | None, status: RunStatus, legal: frozenset[Move]) -> bool:
    if d is None or d.move not in legal:
        return False
    if d.move == Move.ask:
        return status.ask_available and d.question_key in status.available_question_keys
    return True


def make_supervisor(deps: Deps, routers: dict[str, Router]) -> Node:
    shadow_router = CodeRouter()

    def status_of(run_id: str) -> RunStatus:
        return deps.runs_db.build_run_status(run_id, deps.settings, deps.memory_db.is_blocked_or_contacted)

    def supervisor(state: ParentState, config: RunnableConfig) -> Command:
        run_id = state["run_id"]
        db = deps.runs_db
        status = status_of(run_id)
        assumptions: list[str] = []
        # Remembered answers are applied instead of asked, one key at a time (each changes the status).
        while remembered := next(((k, v) for k in sorted(status.available_question_keys)
                                  if (v := deps.memory_db.preference(k.value)) is not None), None):
            key, value = remembered
            db.record_answer(pause_id=f"memory:{run_id}:{key.value}", run_id=run_id, key=key.value, value=value,
                             origin="memory")
            apply_answer(deps, run_id, key, value)
            assumptions.append(f"applied remembered answer for {key.value}")
            status = status_of(run_id)

        legal = legal_moves(status)
        shadow = shadow_router.decide(status, legal)
        if len(legal) == 1:
            decision, decided_by = Decision(move=next(iter(legal)), reason="only legal move"), DecidedBy.forced
        else:
            router_config = {**dict(config), "run_name": "router",
                             "metadata": {**config.get("metadata", {}), "run_id": run_id,
                                          "router": status.router}}
            chosen = routers[status.router].decide(status, legal, run_id=run_id, config=router_config)
            if valid_decision(chosen, status, legal):
                assert chosen is not None
                decision, decided_by = chosen, DecidedBy.router
            else:
                decision, decided_by = shadow, DecidedBy.fallback
        if decision.assumption:
            assumptions.append(decision.assumption)

        update: dict = {}
        if decision.move == Move.ask:
            assert decision.question_key is not None
            update["pending_question"] = question_for(decision.question_key, db.plan_model(run_id),
                                                      deps).model_dump(mode="json")
        db.record_decision(
            run_id=run_id, status_json=status.model_dump_json(), legal_moves=sorted(m.value for m in legal),
            router=status.router, decided_by=decided_by.value, chosen_move=decision.move.value,
            shadow_move=shadow.move.value, reason=decision.reason[:REASON_CHARS],
            question_key=decision.question_key.value if decision.move == Move.ask and decision.question_key
            else None,
            assumption="; ".join(assumptions) or None)
        return Command(goto=ROUTES[decision.move], update=update)

    return supervisor
