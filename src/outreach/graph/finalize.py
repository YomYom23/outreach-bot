"""finalize: the only node that writes the final runs.status."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from outreach.graph import Node
from outreach.models import STOP_REASON_TEXT, ResearchStatus, RunStatus, RunStatusValue, StopReason

if TYPE_CHECKING:
    from outreach.deps import Deps
    from outreach.state import ParentState


def choose_stop_reason(state_reason: str | None, status: RunStatus, has_counted: bool) -> StopReason:
    """The design's rule, in order."""
    if state_reason:
        return StopReason(state_reason)
    if status.complete:
        return StopReason.complete
    if status.decisions_used >= status.max_decisions:
        return StopReason.decision_cap
    if not has_counted:
        return StopReason.no_candidates
    if status.exhausted_reason == "empty_round":
        return StopReason.exhausted_empty_round
    return StopReason.exhausted_rounds


def end_screen(deps: Deps, run_id: str) -> dict[str, Any]:
    db = deps.runs_db
    run = db.get_run(run_id)
    status = db.build_run_status(run_id, deps.settings, deps.memory_db.is_blocked_or_contacted)
    data = db.end_screen_counts(run_id)
    reason = run["stop_reason"] if run is not None else None
    data.update({
        "run_id": run_id,
        "stop_reason": reason,
        "stop_reason_text": STOP_REASON_TEXT[StopReason(reason)] if reason else None,
        "qualified_count": status.qualified_count,
        "target_qualified": status.target_qualified,
        "summary": f"{status.qualified_count} of {status.target_qualified} qualified"
                   + (f": {STOP_REASON_TEXT[StopReason(reason)]}" if reason else ""),
    })
    return data


def make_finalize(deps: Deps) -> Node:
    def finalize(state: ParentState) -> dict:
        run_id = state["run_id"]
        db = deps.runs_db
        status = db.build_run_status(run_id, deps.settings, deps.memory_db.is_blocked_or_contacted)
        has_counted = db.count_candidates(run_id, ResearchStatus.candidate, ResearchStatus.parked_no_email) > 0
        reason = choose_stop_reason(state.get("stop_reason"), status, has_counted)
        db.close_open_round(run_id)
        db.release_open_candidates(run_id)
        deps.memory_db.release(run_id)
        final = RunStatusValue.discarded if reason == StopReason.user_discarded else RunStatusValue.complete
        db.set_run_status(run_id, final, stop_reason=reason.value)
        return {"stop_reason": reason.value}

    return finalize
