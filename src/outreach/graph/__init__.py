"""The parent graph: intake, supervisor, stage wrappers, outbox and finalize."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from outreach.models import ProgressEvent

# A node function. LangGraph's node protocol wants a parameter named `state`, which a
# Callable[[ParentState], ...] type can't express, so factories return this.
Node = Callable[..., Any]


def emit(event: ProgressEvent) -> None:
    """Sends a progress event to the CLI through LangGraph's custom stream. A no-op outside a run."""
    try:
        from langgraph.config import get_stream_writer

        writer = get_stream_writer()
    except Exception:  # called outside a graph run (unit tests)
        return
    writer(event.model_dump(mode="json"))


def invoke_subgraph(graph: Any, value: Any, config: dict) -> Any:
    """Invokes a `checkpointer=False` subgraph from inside a parent node.

    The parent streams with `durability="sync"`, and a subgraph inherits that through its config. With no
    checkpointer it then waits on a checkpoint it never writes, and crashes (langgraph 1.2.21; see
    docs/vendor_notes.md, Phase 6). An explicit `durability="exit"` overrides the inherited value. LangGraph
    warns that durability "has no effect" without a checkpointer; here it does, so that one warning is muted.
    """
    import warnings

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="`durability` has no effect", category=UserWarning)
        return graph.invoke(value, config, durability="exit")
