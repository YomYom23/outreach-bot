"""Run configs for LangGraph/LangSmith, and trace flushing.

LangSmith groups runs into a thread by the `thread_id` (or `session_id`) metadata key
(docs/vendor_notes.md), so every config carries `thread_id = run_id`.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from outreach.config import Settings
    from outreach.models import RunStatus


def tracing_enabled() -> bool:
    return bool(os.environ.get("LANGSMITH_API_KEY")) and \
        os.environ.get("LANGSMITH_TRACING", "").lower() == "true"


def disable_tracing_if_unconfigured() -> None:
    """LangChain reads LANGSMITH_TRACING itself; force it off when no key is set."""
    if not os.environ.get("LANGSMITH_API_KEY"):
        os.environ["LANGSMITH_TRACING"] = "false"


def recursion_limit(settings: Settings, status: RunStatus | None) -> int:
    """recursion_margin + rounds × companies × steps_per_company + drafts × steps_per_draft × outbox_passes.

    Over-counts: subgraph steps are counted per invocation, not against the parent (vendor_notes.md).
    """
    max_rounds = status.max_research_rounds if status else settings.max_research_rounds
    target = status.target_qualified if status else settings.target_qualified
    drafts = max(status.qualified_count, target) if status else target
    return (settings.recursion_margin
            + max_rounds * settings.max_companies_examined_per_round * settings.steps_per_company
            + drafts * settings.steps_per_draft * settings.outbox_passes)


def run_config(run_id: str, router: str, settings: Settings, status: RunStatus | None = None) -> dict[str, Any]:
    """Recomputed for every `stream()` call."""
    return {
        "configurable": {"thread_id": run_id},
        "metadata": {"run_id": run_id, "thread_id": run_id, "router": router},
        "recursion_limit": recursion_limit(settings, status),
    }


def child_config(parent: dict[str, Any], *, round_: int | None, lead_id: str | None = None,
                 run_name: str | None = None) -> dict[str, Any]:
    """Per-company/sub-invoke config: same thread and limit, more metadata, a readable run name."""
    meta = {**parent.get("metadata", {}), "round": round_}
    if lead_id is not None:
        meta["lead_id"] = lead_id
    configurable = {**parent.get("configurable", {}), "run_id": meta.get("run_id"), "round": round_}
    child: dict[str, Any] = {
        "configurable": configurable,
        "metadata": meta,
        "recursion_limit": parent.get("recursion_limit"),
    }
    if run_name:
        child["run_name"] = run_name
    return child


def flush() -> None:
    """Sends pending traces before the process exits. A no-op when tracing is off."""
    if not tracing_enabled():
        return
    from langchain_core.tracers.langchain import wait_for_all_tracers

    wait_for_all_tracers()
