"""Shared fixtures. Sockets are blocked (pyproject addopts) except for 127.0.0.1 and `live` tests."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

os.environ["LANGSMITH_TRACING"] = "false"
os.environ.pop("LANGSMITH_API_KEY", None)

from outreach.config import ModelPrice, Settings, TargetProfile, _profile_from_raw  # noqa: E402
from outreach.db.memory_db import MemoryDB  # noqa: E402
from outreach.db.runs_db import RunsDB  # noqa: E402
from outreach.deps import Deps  # noqa: E402
from tests.fakes import FakeClock, FakeExa, FakeFetcher, FakeHunter, RecordingMailer, SeqIds  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if os.environ.get("OUTREACH_LIVE") == "1":
        return
    skip = pytest.mark.skip(reason="live test: set OUTREACH_LIVE=1")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _live_sockets(request: pytest.FixtureRequest) -> Iterator[None]:
    if "live" in request.keywords:
        import pytest_socket
        pytest_socket.enable_socket()
        # --allow-hosts patches connect() separately; enable_socket() alone leaves it (found by live/test_smoke).
        pytest_socket._remove_restrictions()
    yield


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "testing": True,
        "user_agent": "OutreachBot/0.1 (+https://github.com/test/out-reach-bot)",
        "runs_db_path": tmp_path / "runs.db",
        "memory_db_path": tmp_path / "memory.db",
        "model_prices": {"claude-haiku-5-5": ModelPrice(input=0.10, output=0.50),
                         "claude-opus-5-5": ModelPrice(input=4.0, output=20.0)},
    }
    base.update(overrides)
    return Settings.model_validate(base)


def load_profile() -> TargetProfile:
    with open(REPO / "config" / "target_roofing.toml", "rb") as f:
        raw = tomllib.load(f)
    return _profile_from_raw(raw, ("Tucson, AZ",))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
def runs_db(settings: Settings, clock: FakeClock) -> Iterator[RunsDB]:
    db = RunsDB(settings.runs_db_path, now=clock.now)
    yield db
    db.close()


@pytest.fixture
def memory_db(settings: Settings, clock: FakeClock) -> Iterator[MemoryDB]:
    db = MemoryDB(settings.memory_db_path, now=clock.now, rejected_retry_days=settings.rejected_retry_days)
    db.migrate(None)
    yield db
    db.close()


@pytest.fixture
def deps_fake(settings: Settings, runs_db: RunsDB, memory_db: MemoryDB, clock: FakeClock) -> Deps:
    return Deps(settings=settings, profile=load_profile(), runs_db=runs_db, memory_db=memory_db,
                llms={}, exa=FakeExa(), fetcher=FakeFetcher(), hunter=FakeHunter(),
                mailer=RecordingMailer(), clock=clock, ids=SeqIds())


@pytest.fixture
def graph_fake(deps_fake: Deps) -> Callable[..., tuple[Any, Any, Any]]:
    """graph_fake(rounds=None, test_hooks=False, ...) -> (graph, stages, Driver) over deps_fake."""
    from tests.helpers import Driver, make_graph

    def make(rounds: Any = None, **kw: Any) -> tuple[Any, Any, Any]:
        graph, stages = make_graph(deps_fake, rounds, **kw)
        return graph, stages, Driver(graph, deps_fake)

    return make
