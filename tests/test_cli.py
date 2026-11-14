"""Phase 3 exit test: the CLI driver, run lifecycle and exit codes."""

from __future__ import annotations

import io
from types import SimpleNamespace
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from outreach import __version__
from outreach import cli as cli_mod
from outreach.clients import VendorAuthError
from outreach.deps import Deps
from outreach.models import PausePayload, PlanAnswer, QuestionAnswer, ResearchStatus, ReviewAnswer, RunStatusValue
from tests.fakes import FakeChat, raises
from tests.helpers import INTAKE, drafter_chat, make_graph, parse_chat


def auto_answer(p: PausePayload) -> Any:
    if p.kind == "question":
        value = INTAKE.get(p.context["key"], "no")
        return QuestionAnswer(pause_id=p.pause_id, key=p.context["key"], value=value)
    if p.kind == "plan":
        if not p.context["threshold_valid"]:
            return PlanAnswer(pause_id=p.pause_id, action="edit", edits={"threshold": "2"})
        return PlanAnswer(pause_id=p.pause_id, action="confirm")
    return ReviewAnswer(pause_id=p.pause_id, draft_id=p.context["draft_id"], shown_hash=p.context["shown_hash"],
                        action="approve")


class ScriptedScreens:
    """Answers pauses automatically. After `budget` answers it presses Ctrl+C."""

    def __init__(self) -> None:
        self.console = Console(file=io.StringIO(), width=200)
        self.inputs: list[str] = []
        self.asked: list[PausePayload] = []
        self.ended: list[dict] = []
        self.budget: int | None = None

    def read(self, prompt: str) -> str:
        return self.inputs.pop(0) if self.inputs else ""

    def ask(self, payload: PausePayload) -> Any:
        if self.budget is not None and len(self.asked) >= self.budget:
            raise KeyboardInterrupt
        self.asked.append(payload)
        return auto_answer(payload)

    def progress(self, chunk: Any) -> None:
        pass

    def end(self, data: dict) -> None:
        self.ended.append(data)

    def banner(self, run: Any, status: Any) -> None:
        self.console.print(f"banner {run['run_id']}")

    @property
    def output(self) -> str:
        assert isinstance(self.console.file, io.StringIO)
        return self.console.file.getvalue()


@pytest.fixture
def cli(deps_fake: Deps, monkeypatch: pytest.MonkeyPatch) -> Any:
    state = SimpleNamespace(screens=ScriptedScreens(), parse=parse_chat(), drafter=drafter_chat(), deps=deps_fake)

    def build(deps: Deps) -> Any:
        graph, _ = make_graph(deps, parse=state.parse, drafter=state.drafter)
        return graph

    monkeypatch.setattr(cli_mod, "make_runtime",
                        lambda: cli_mod.Runtime(deps=deps_fake, build=build, screens=state.screens))
    runner = CliRunner()
    state.invoke = lambda *args: runner.invoke(cli_mod.app, list(args))
    return state


def run_row(deps: Deps, run_id: str = "id-0001") -> Any:
    return deps.runs_db.get_run(run_id)


def checkpoint(deps: Deps, run_id: str) -> Any:
    return deps.runs_db.saver.get_tuple({"configurable": {"thread_id": run_id}})


def test_full_run_exits_0_with_end_screen(cli: Any) -> None:
    result = cli.invoke("run", "find me leads in Tucson that don't use AI", "--router", "code")
    assert result.exit_code == 0, cli.screens.output
    [end] = cli.screens.ended
    assert end["summary"] == "3 of 4 qualified: research rounds used up"
    assert end["funnel"]["sent"] == 3
    assert run_row(cli.deps)["status"] == "complete"


def test_bad_input_exit_2(cli: Any) -> None:
    assert cli.invoke("run").exit_code == 2  # no prompt
    assert cli.invoke("resume").exit_code == 2  # nothing to resume
    assert cli.invoke("run", "x", "--router", "gpt").exit_code == 2
    assert cli.invoke("leads", "--status", "nope").exit_code == 2


def test_version_mismatch_refuses_but_discard_works(cli: Any) -> None:
    cli.deps.runs_db.create_run(run_id="OLD", app_version="0.0.1", router="code", prompt="p")
    assert cli.invoke("resume").exit_code == 4
    cli.screens.inputs = ["resume"]
    assert cli.invoke("run").exit_code == 4
    assert cli.invoke("discard").exit_code == 0
    assert run_row(cli.deps, "OLD")["status"] == "discarded"


def test_discard_releases_claims_and_checkpoints(cli: Any) -> None:
    cli.screens.budget = 0  # Ctrl+C at the first pause
    assert cli.invoke("run", "find leads", "--router", "code").exit_code == 130
    assert run_row(cli.deps)["status"] == "paused"
    assert checkpoint(cli.deps, "id-0001") is not None
    cli.deps.memory_db.claim("held.test", "id-0001", None)
    cli.deps.runs_db.queue_candidates("id-0001", 1, ["held.test"])

    assert cli.invoke("discard").exit_code == 0
    row = run_row(cli.deps)
    assert (row["status"], row["stop_reason"]) == ("discarded", "user_discarded")
    assert cli.deps.memory_db.lead("held.test")["research_status"] == ResearchStatus.released
    assert cli.deps.runs_db.candidate("id-0001", "held.test")["status"] == ResearchStatus.released
    assert checkpoint(cli.deps, "id-0001") is None


def test_run_with_unfinished_offers_discard(cli: Any) -> None:
    cli.deps.runs_db.create_run(run_id="R0", app_version=__version__, router="code", prompt="p")
    cli.screens.inputs = ["discard"]
    assert cli.invoke("run").exit_code == 0
    assert "banner R0" in cli.screens.output
    assert run_row(cli.deps, "R0")["status"] == "discarded"


def test_router_mismatch_on_resume(cli: Any) -> None:
    cli.deps.runs_db.create_run(run_id="R0", app_version=__version__, router="code", prompt="p")
    assert cli.invoke("resume", "--router", "claude").exit_code == 2
    assert run_row(cli.deps, "R0")["status"] == "active"


def test_purge_refused_during_a_run(cli: Any) -> None:
    deps = cli.deps
    deps.memory_db.block_many(["gone.test"], "R-old")
    deps.runs_db.create_run(run_id="R0", app_version=__version__, router="code", prompt="p")
    assert cli.invoke("purge", "gone.test").exit_code == 2
    assert deps.memory_db.is_blocked_or_contacted("gone.test")
    assert cli.invoke("discard").exit_code == 0
    assert cli.invoke("purge", "Gone.test").exit_code == 0
    assert not deps.memory_db.is_blocked_or_contacted("gone.test")


def test_leads_lists_blocked(cli: Any) -> None:
    cli.deps.memory_db.block_many(["bad.test"], "R-old")
    result = cli.invoke("leads", "--status", "blocked")
    assert result.exit_code == 0 and "bad.test" in cli.screens.output


@pytest.mark.parametrize("args", [("leads",), ("purge", "x.test")])
def test_fresh_install_migrates_before_reading(cli: Any, tmp_path: Any, args: tuple[str, ...]) -> None:
    """Phase 7 finding: `leads` on a never-migrated memory.db crashed with 'no such table: leads'."""
    from outreach.db.memory_db import MemoryDB

    fresh = MemoryDB(tmp_path / "fresh-memory.db", now=cli.deps.clock.now, rejected_retry_days=30)
    cli.deps.memory_db = fresh
    try:
        assert cli.invoke(*args).exit_code == 0
        assert fresh.user_version > 0
    finally:
        fresh.close()


def test_vendor_auth_error_pauses_and_is_resumable(cli: Any) -> None:
    cli.parse = FakeChat([raises(VendorAuthError("Anthropic", 401, "ANTHROPIC_API_KEY"))],
                         responder=parse_chat().responder)
    assert cli.invoke("run", "find leads in Tucson", "--router", "code").exit_code == 1
    assert run_row(cli.deps)["status"] == "paused"
    assert "ANTHROPIC_API_KEY" in cli.screens.output

    assert cli.invoke("resume").exit_code == 0
    assert run_row(cli.deps)["status"] == "complete"
    assert cli.screens.asked[0].pause_id == "q:id-0001:sender_name"


def test_failed_run_is_resumable(cli: Any, tmp_path: Any) -> None:
    fired = []

    def crash_once(run_id: str, lead_id: str, status: ResearchStatus) -> None:
        if status == ResearchStatus.candidate and lead_id == "best-roof.test" and not fired:
            fired.append(lead_id)
            raise RuntimeError("disk on fire")

    cli.deps.runs_db.fault_hook = crash_once
    assert cli.invoke("run", "find leads in Tucson", "--router", "code").exit_code == 1
    assert run_row(cli.deps)["status"] == "failed"
    assert "disk on fire" in cli.screens.output
    assert (tmp_path / "trace-id-0001.txt").exists()

    assert cli.invoke("resume").exit_code == 0
    assert run_row(cli.deps)["status"] == "complete"
    assert cli.screens.ended[0]["funnel"]["counted"] == 5  # nothing lost or double counted
    round1 = cli.deps.runs_db.conn.execute(
        "SELECT new_companies FROM research_rounds WHERE run_id = 'id-0001' AND round = 1").fetchone()
    assert round1[0] == 3


def _city_run(deps: Any, run_id: str, city: str, domains: list[str]) -> None:
    db = deps.runs_db
    db.create_run(run_id=run_id, app_version=__version__, router="code", prompt="p")
    db.write_plan(run_id, version=1, status="frozen", city=city, target_qualified=3,
                  checklist={"items": [], "threshold": None}, sender={})
    db.set_run_status(run_id, RunStatusValue.complete)
    with db.conn:
        db.conn.executemany(
            "INSERT INTO run_candidates (run_id, lead_id, round, queue_pos, status) VALUES (?, ?, 1, ?, 'done')",
            [(run_id, d, i) for i, d in enumerate(domains)])


def test_purge_city_removes_only_that_citys_companies(cli: Any) -> None:
    deps = cli.deps
    _city_run(deps, "R-tuc", "Tucson, AZ", ["a.test", "b.test"])
    _city_run(deps, "R-phx", "Phoenix", ["p.test"])
    deps.memory_db.block_many(["a.test", "c.test"], "R-tuc")  # c.test: only in memory.db
    deps.memory_db.block_many(["p.test"], "R-phx")

    result = cli.invoke("purge", "tucson", "--yes")
    assert result.exit_code == 0, cli.screens.output
    assert "Purged 3 companies for tucson" in cli.screens.output
    assert deps.runs_db.candidate_domains(["R-tuc"]) == set()
    assert not deps.memory_db.is_blocked_or_contacted("a.test")
    assert not deps.memory_db.is_blocked_or_contacted("c.test")
    assert deps.runs_db.candidate_domains(["R-phx"]) == {"p.test"}
    assert deps.memory_db.is_blocked_or_contacted("p.test")


def test_purge_city_asks_first(cli: Any) -> None:
    _city_run(cli.deps, "R-tuc", "Tucson", ["a.test"])
    result = CliRunner().invoke(cli_mod.app, ["purge", "Tucson"], input="n\n")
    assert result.exit_code == 0
    assert "Nothing purged." in cli.screens.output
    assert cli.deps.runs_db.candidate_domains(["R-tuc"]) == {"a.test"}


def test_purge_unknown_city_lists_known_ones(cli: Any) -> None:
    _city_run(cli.deps, "R-tuc", "Tucson", ["a.test"])
    assert cli.invoke("purge", "Tuscon").exit_code == 2
    assert "Known cities: Tucson" in cli.screens.output


def test_tracing_key_only_in_dotenv_keeps_tracing_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: the tracing check ran before .env was loaded and forced LANGSMITH_TRACING=false."""
    import os

    from outreach import config
    from outreach.tracing import tracing_enabled

    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    def fake_load() -> tuple[None, None, None]:  # what load_dotenv(override=False) does with a .env key
        os.environ.setdefault("LANGSMITH_API_KEY", "lsv2-test")
        os.environ.setdefault("LANGSMITH_TRACING", "true")
        return None, None, None

    monkeypatch.setattr(config, "load", fake_load)
    cli_mod.load_config()
    assert tracing_enabled()


def test_tracing_forced_off_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from outreach import config

    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setattr(config, "load", lambda: (None, None, None))
    cli_mod.load_config()
    assert os.environ["LANGSMITH_TRACING"] == "false"
