"""build_graph: wires the parent graph (design: Components, How a run flows)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from outreach.graph.finalize import make_finalize
from outreach.graph.intake import make_confirm_plan, make_fill_from_memory, make_intake_missing, make_parse_request
from outreach.graph.outbox import make_drafter, make_send_next
from outreach.graph.pauses import make_ask_user, make_review_next
from outreach.graph.routers import Router, default_routers
from outreach.graph.supervisor import ROUTES, make_supervisor
from outreach.qualify.subgraph import make_run_qualify
from outreach.research.subgraph import make_run_research
from outreach.state import ParentState

if TYPE_CHECKING:
    from outreach.deps import Deps

StageNode = Callable[..., Any]


def build_graph(deps: Deps, stages: dict[str, StageNode] | None = None, test_hooks: bool = False,
                routers: dict[str, Router] | None = None) -> Any:
    """Compiles the parent graph with the runs.db SqliteSaver.

    `stages` replaces `run_research` and/or `run_qualify` (tests/canned_stages.py). With `test_hooks=True`,
    `confirm_plan` exits to `supervisor_override`, which jumps straight to `send_next` (test_send_guard).
    """
    stages = stages or {}
    after_confirm = "supervisor_override" if test_hooks else "supervisor"
    g = StateGraph(ParentState)
    g.add_node("parse_request", make_parse_request(deps))
    g.add_node("fill_from_memory", make_fill_from_memory(deps))
    g.add_node("intake_missing", make_intake_missing(deps), destinations=("ask_user", "confirm_plan"))
    g.add_node("ask_user", make_ask_user(deps), destinations=("intake_missing", "supervisor"))
    g.add_node("confirm_plan", make_confirm_plan(deps, after_confirm), destinations=("confirm_plan", after_confirm))
    g.add_node("supervisor", make_supervisor(deps, routers or default_routers(deps)),
               destinations=tuple(ROUTES.values()))
    g.add_node("run_research", stages.get("run_research") or make_run_research(deps))
    g.add_node("run_qualify", stages.get("run_qualify") or make_run_qualify(deps))
    g.add_node("drafter", make_drafter(deps), destinations=("review_next",))
    g.add_node("review_next", make_review_next(deps), destinations=("review_next", "send_next"))
    g.add_node("send_next", make_send_next(deps), destinations=("send_next", "supervisor"))
    g.add_node("finalize", make_finalize(deps))
    if test_hooks:
        g.add_node("supervisor_override", lambda _s: Command(goto="send_next"), destinations=("send_next",))

    g.add_edge(START, "parse_request")
    g.add_edge("parse_request", "fill_from_memory")
    g.add_edge("fill_from_memory", "intake_missing")
    g.add_edge("run_research", "supervisor")
    g.add_edge("run_qualify", "supervisor")
    g.add_edge("finalize", END)
    return g.compile(checkpointer=deps.runs_db.saver)
