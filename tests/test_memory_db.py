"""Phase 2 exit test: memory.db migrations, should_skip, claim, blocks and purge."""

from __future__ import annotations

import shutil
import sqlite3
from importlib import resources
from pathlib import Path

import pytest

from outreach.db.memory_db import MemoryDB, MigrationRefused, checklist_key
from outreach.db.runs_db import RunsDB
from outreach.models import ResearchStatus, RunStatusValue
from tests.fakes import FakeClock

KEY_A = checklist_key([{"text": "No AI", "check": "code"}], 1)
KEY_B = checklist_key([{"text": "No AI", "check": "code"}], 2)
ALL = [1, 2]  # every shipped migration
LATEST = ALL[-1]


def schema_copy(tmp_path: Path, extra: dict[str, str] | None = None) -> Path:
    src = resources.files("outreach.db").joinpath("memory_schema")
    dest = tmp_path / "schema"
    dest.mkdir()
    for entry in src.iterdir():
        if entry.name.endswith(".sql"):
            (dest / entry.name).write_text(entry.read_text(encoding="utf-8"), encoding="utf-8")
    for name, sql in (extra or {}).items():
        (dest / name).write_text(sql, encoding="utf-8")
    return dest


def runs_with(tmp_path: Path, status: RunStatusValue) -> Path:
    path = tmp_path / "runs.db"
    db = RunsDB(path)
    db.create_run(run_id="R1", app_version="0.1.0", router="code", prompt="p")
    db.set_run_status("R1", status)
    db.close()
    return path


# --- migrations -----------------------------------------------------------------


def test_fresh_migration_applies_v1_without_backup(tmp_path: Path) -> None:
    db = MemoryDB(tmp_path / "memory.db")
    assert db.migrate(None) == ALL
    assert db.user_version == LATEST
    assert not list(tmp_path.glob("memory.db.bak-*"))
    assert db.migrate(None) == []  # idempotent


def test_migration_backs_up_first(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    db = MemoryDB(path)
    db.migrate(None)
    db.block_many(["acme.test"], "R0")
    schema = schema_copy(tmp_path, {"090_notes.sql": "ALTER TABLE leads ADD COLUMN notes TEXT;"})

    assert db.migrate(None, schema_dir=schema) == [90]
    assert db.user_version == 90

    backup = sqlite3.connect(tmp_path / f"memory.db.bak-{LATEST}")
    assert backup.execute("PRAGMA user_version").fetchone()[0] == LATEST
    assert backup.execute("SELECT domain_key FROM do_not_contact").fetchall() == [("acme.test",)]
    assert "notes" not in [r[1] for r in backup.execute("PRAGMA table_info(leads)")]
    backup.close()


def test_failed_migration_rolls_back_every_file(tmp_path: Path) -> None:
    db = MemoryDB(tmp_path / "memory.db")
    db.migrate(None)
    schema = schema_copy(tmp_path, {"090_ok.sql": "ALTER TABLE leads ADD COLUMN notes TEXT;",
                                    "091_bad.sql": "ALTER TABLE nope ADD COLUMN x TEXT;"})
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(None, schema_dir=schema)
    assert db.user_version == LATEST
    assert "notes" not in [r[1] for r in db.conn.execute("PRAGMA table_info(leads)")]


@pytest.mark.parametrize("status", [RunStatusValue.active, RunStatusValue.paused, RunStatusValue.failed])
def test_pending_migration_refused_while_run_unfinished(tmp_path: Path, status: RunStatusValue) -> None:
    runs_path = runs_with(tmp_path, status)
    db = MemoryDB(tmp_path / "memory.db")
    with pytest.raises(MigrationRefused, match="R1"):
        db.migrate(runs_path)
    assert db.user_version == 0


def test_no_refusal_when_nothing_pending(tmp_path: Path) -> None:
    db = MemoryDB(tmp_path / "memory.db")
    db.migrate(None)
    runs_path = runs_with(tmp_path, RunStatusValue.active)
    assert db.migrate(runs_path) == []


def test_finished_runs_do_not_block_migration(tmp_path: Path) -> None:
    runs_path = runs_with(tmp_path, RunStatusValue.complete)
    db = MemoryDB(tmp_path / "memory.db")
    assert db.migrate(runs_path) == ALL


def test_missing_runs_db_means_no_runs(tmp_path: Path) -> None:
    db = MemoryDB(tmp_path / "memory.db")
    assert db.migrate(tmp_path / "does-not-exist.db") == ALL
    assert not (tmp_path / "does-not-exist.db").exists()


# --- should_skip ----------------------------------------------------------------


def set_lead(db: MemoryDB, domain: str, status: ResearchStatus, checklist: str | None = None) -> None:
    assert db.claim(domain, "R0", None)
    db.finalize(domain, "R0", status)
    if checklist:
        db.record_qualification(domain, checklist)


@pytest.mark.parametrize(
    ("setup", "age_days", "run_key", "expected"),
    [
        ("unknown", 0, KEY_A, None),
        ("dnc", 0, KEY_A, "do_not_contact"),
        ("contacted", 0, KEY_A, "contacted"),
        ("rejected_not_roofer", 0, KEY_A, "recently_rejected"),
        ("rejected_triage", 89, KEY_A, "recently_rejected"),
        ("rejected_non_us", 91, KEY_A, None),
        ("candidate:A", 0, KEY_A, "already_researched"),
        ("candidate:A", 0, KEY_B, None),
        ("parked_no_email:A", 400, KEY_A, "already_researched"),
        ("parked_no_email:B", 0, KEY_A, None),
        ("candidate:none", 0, KEY_A, None),
        ("released", 0, KEY_A, None),
        ("researching", 0, KEY_A, None),
    ],
)
def test_should_skip_table(memory_db: MemoryDB, clock: FakeClock, setup: str, age_days: int,
                           run_key: str, expected: str | None) -> None:
    d = "acme-roofing.test"
    status, _, key = setup.partition(":")
    if status == "dnc":
        memory_db.block_many([d], "R0")
    elif status == "contacted":
        memory_db.mark_contacted(d, "R0", "draft-1")
    elif status != "unknown":
        checklist = {"A": KEY_A, "B": KEY_B}.get(key)
        set_lead(memory_db, d, ResearchStatus(status), checklist)
    clock.advance(days=age_days)

    assert memory_db.skip_reason(d, run_key) == expected
    assert memory_db.should_skip(d, run_key) is (expected is not None)


# --- claim, release, blocks, purge -------------------------------------------------


def test_claim_marks_researching_and_refuses_skipped(memory_db: MemoryDB) -> None:
    assert memory_db.claim("new.test", "R1", KEY_A)
    row = memory_db.lead("new.test")
    assert row is not None
    assert (row["research_status"], row["first_run_id"], row["last_run_id"]) == ("researching", "R1", "R1")

    memory_db.block_many(["blocked.test"], "R0")
    assert not memory_db.claim("blocked.test", "R1", KEY_A)
    assert memory_db.lead("blocked.test") is None


def test_reclaim_in_same_run_after_crash_is_allowed(memory_db: MemoryDB) -> None:
    assert memory_db.claim("crash.test", "R1", KEY_A)
    assert memory_db.claim("crash.test", "R1", KEY_A)  # `researching` is not a skip state


def test_reject_blocks_future_runs(memory_db: MemoryDB) -> None:
    assert memory_db.claim("rejected.test", "R1", KEY_A)
    memory_db.finalize("rejected.test", "R1", ResearchStatus.candidate)
    memory_db.block_many(["rejected.test"], "R1", reason="rejected")
    for run_id, key in (("R2", KEY_A), ("R3", KEY_B)):
        assert not memory_db.claim("rejected.test", run_id, key)
    assert memory_db.is_blocked_or_contacted("rejected.test")


def test_release_only_touches_this_runs_unfinished_claims(memory_db: MemoryDB) -> None:
    memory_db.claim("mine-open.test", "R1", KEY_A)
    memory_db.claim("mine-done.test", "R1", KEY_A)
    memory_db.finalize("mine-done.test", "R1", ResearchStatus.candidate)
    memory_db.claim("other.test", "R2", KEY_A)

    assert memory_db.release("R1") == 1
    statuses = {d: memory_db.lead(d)["research_status"]  # type: ignore[index]
                for d in ("mine-open.test", "mine-done.test", "other.test")}
    assert statuses == {"mine-open.test": "released", "mine-done.test": "candidate",
                        "other.test": "researching"}


def test_purge_removes_company_from_both_databases(memory_db: MemoryDB, runs_db: RunsDB) -> None:
    d = "purge-me.test"
    memory_db.claim(d, "R1", KEY_A)
    memory_db.block_many([d], "R1")
    memory_db.mark_contacted(d, "R1", "draft-1")
    runs_db.create_run(run_id="R1", app_version="0.1.0", router="code", prompt="p")
    runs_db.queue_candidates("R1", 1, [d, "keep.test"])
    runs_db.hunter_cache_put(d, {"data": {}})

    memory_db.purge(d)
    runs_db.purge_domain(d)

    assert memory_db.lead(d) is None
    assert not memory_db.is_blocked_or_contacted(d)
    assert memory_db.claim(d, "R2", KEY_A)  # researchable again
    remaining = [r[0] for r in runs_db.conn.execute("SELECT lead_id FROM run_candidates")]
    assert remaining == ["keep.test"]
    assert runs_db.hunter_cache_get(d) is None


def test_remembered_rows_latest_wins(memory_db: MemoryDB, clock: FakeClock) -> None:
    memory_db.remember_sender("Ann", "Ann Co", "reply")
    clock.advance(minutes=1)
    memory_db.remember_sender("Bob", "Bob Co", "call")
    sender = memory_db.latest_sender()
    assert sender is not None and sender["name"] == "Bob"

    items = [{"text": "No AI", "check": "code"}]
    key = memory_db.remember_checklist(items, 1)
    checklist = memory_db.latest_checklist()
    assert checklist is not None and checklist["checklist_key"] == key == KEY_A

    memory_db.remember_preference("extra_round", "yes")
    assert memory_db.preference("extra_round") == "yes"
    assert memory_db.preference("change_city") is None


def test_checklist_key_ignores_order_and_spacing() -> None:
    a = checklist_key([{"text": "No  AI", "check": "code"}, {"text": "Owner", "check": "llm"}], 2)
    b = checklist_key([{"text": "owner", "check": "llm"}, {"text": "no ai", "check": "code"}], 2)
    assert a == b
    assert a != checklist_key([{"text": "Owner", "check": "llm"}, {"text": "No AI", "check": "code"}], 1)


def test_backup_copy_is_a_valid_database(tmp_path: Path) -> None:
    # The backup must be openable on its own (WAL content included).
    path = tmp_path / "memory.db"
    db = MemoryDB(path)
    db.migrate(None)
    db.remember_preference("k", "v")
    db.migrate(None, schema_dir=schema_copy(tmp_path, {"090_x.sql": "CREATE TABLE x (a INTEGER);"}))
    db.close()
    copy = tmp_path / "restored.db"
    shutil.copy(tmp_path / f"memory.db.bak-{LATEST}", copy)
    restored = MemoryDB(copy)
    assert restored.preference("k") == "v"
    assert restored.user_version == LATEST


# --- lead city ------------------------------------------------------------------


def test_claim_records_first_city_only(memory_db: MemoryDB) -> None:
    assert memory_db.claim("roof.test", "R1", None, city="Tucson, AZ ")
    memory_db.finalize("roof.test", "R1", ResearchStatus.released)
    assert memory_db.claim("roof.test", "R2", KEY_A, city="Phoenix")  # re-claimed by a Phoenix run
    lead = memory_db.lead("roof.test")
    assert lead is not None and lead["city"] == "Tucson, AZ"
    assert memory_db.domains_for_city("tucson") == {"roof.test"}
    assert memory_db.domains_for_city("Phoenix") == set()


def test_migration_2_backfills_city_from_runs_db(tmp_path: Path) -> None:
    runs_path = tmp_path / "runs.db"
    runs = RunsDB(runs_path)
    runs.create_run(run_id="R1", app_version="0.1.0", router="code", prompt="p")
    runs.write_plan("R1", version=1, status="draft", city="Mesa", target_qualified=1,
                    checklist={"items": [], "threshold": None}, sender={})
    runs.write_plan("R1", version=2, status="frozen", city="Tucson", target_qualified=1,
                    checklist={"items": [], "threshold": None}, sender={})
    runs.set_run_status("R1", RunStatusValue.complete)
    runs.close()

    schema_v1 = tmp_path / "v1"
    schema_v1.mkdir()
    src = resources.files("outreach.db").joinpath("memory_schema", "001_init.sql")
    (schema_v1 / "001_init.sql").write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
    db = MemoryDB(tmp_path / "memory.db")
    db.migrate(runs_path, schema_dir=schema_v1)
    db.conn.execute("INSERT INTO leads (domain_key, research_status, first_run_id, last_run_id,"
                    " normalization_version, updated_at) VALUES ('old.test', 'qualified', 'R1', 'R1', 1, 'x'),"
                    " ('orphan.test', 'qualified', 'R-gone', 'R-gone', 1, 'x')")
    db.conn.commit()

    assert db.migrate(runs_path) == [2]
    assert db.lead("old.test")["city"] == "Tucson"  # latest plan version wins
    assert db.lead("orphan.test")["city"] is None
