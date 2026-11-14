"""Graph state types. Checkpointed values stay JSON-native (see docs/vendor_notes.md)."""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class ParentState(TypedDict, total=False):
    run_id: str
    # A dumped PendingQuestion, never the Pydantic object.
    pending_question: dict | None
    stop_reason: str | None


class ResearchState(TypedDict, total=False):
    discovery_messages: Annotated[list[AnyMessage], add_messages]
    cursor: int
    stop_reason: str | None  # the StageStopReason next_company ended on


class QualifyState(TypedDict, total=False):
    cursor: int
