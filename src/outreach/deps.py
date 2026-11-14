"""Everything nodes need, injected once at `build_graph` so tests can swap in fakes."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from ulid import ULID

if TYPE_CHECKING:
    from outreach.config import Settings, TargetProfile
    from outreach.db.memory_db import MemoryDB
    from outreach.db.runs_db import RunsDB


class Clock(Protocol):
    def now(self) -> datetime: ...
    def monotonic(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def new_ulid() -> str:
    return str(ULID())


@dataclass
class Deps:
    settings: Settings
    profile: TargetProfile
    runs_db: RunsDB
    memory_db: MemoryDB
    llms: dict[str, Any] = field(default_factory=dict)  # call site -> ChatAnthropic (or a fake)
    exa: Any = None
    fetcher: Any = None
    hunter: Any = None
    mailer: Any = None
    clock: Clock = field(default_factory=SystemClock)
    ids: Callable[[], str] = new_ulid
