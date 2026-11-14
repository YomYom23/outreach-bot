"""Access to memory.db, the cross-run registry. Only this module writes the registry."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from outreach.db import city_key, connect
from outreach.models import REJECTED_STATUSES, UNFINISHED_RUN_STATUSES, ResearchStatus

NORMALIZATION_VERSION = 1
_MIGRATION_NAME = re.compile(r"^(\d{3})_.+\.sql$")


class MigrationRefused(Exception):
    """A migration is pending while a run is unfinished."""


def checklist_key(items: Iterable[dict], threshold: int | None) -> str:
    """sha256 of the normalized items (sorted text and check type) plus the threshold."""
    norm = sorted((" ".join(str(i["text"]).split()).casefold(), str(i["check"])) for i in items)
    return hashlib.sha256(json.dumps({"items": norm, "threshold": threshold}).encode()).hexdigest()


def _migrations(schema_dir: Traversable | Path) -> list[tuple[int, Traversable | Path]]:
    found = []
    for entry in schema_dir.iterdir():
        m = _MIGRATION_NAME.match(entry.name)
        if m:
            found.append((int(m.group(1)), entry))
    return sorted(found, key=lambda t: t[0])


def _unfinished_run_ids(runs_db_path: Path | None) -> list[str]:
    if runs_db_path is None or not Path(runs_db_path).exists():
        return []  # a missing runs.db means there are no runs
    conn = connect(runs_db_path, read_only=True)
    try:
        has_runs = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'runs'").fetchone()
        if not has_runs:
            return []
        marks = ",".join("?" * len(UNFINISHED_RUN_STATUSES))
        return [r[0] for r in conn.execute(f"SELECT run_id FROM runs WHERE status IN ({marks})",
                                           tuple(s.value for s in UNFINISHED_RUN_STATUSES))]
    finally:
        conn.close()


def _plan_cities(runs_db_path: Path | None) -> dict[str, str]:
    """run_id -> city from each run's latest plan in runs.db; empty if runs.db or the table is missing."""
    if runs_db_path is None or not Path(runs_db_path).exists():
        return {}
    conn = connect(runs_db_path, read_only=True)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'run_plans'").fetchone():
            return {}
        rows = conn.execute(
            "SELECT p.run_id, p.city FROM run_plans p"
            " JOIN (SELECT run_id, MAX(plan_version) AS v FROM run_plans GROUP BY run_id) m"
            " ON p.run_id = m.run_id AND p.plan_version = m.v WHERE p.city IS NOT NULL AND p.city != ''")
        return {r[0]: r[1].strip() for r in rows}
    finally:
        conn.close()


def _backfill_lead_city(conn: sqlite3.Connection, runs_db_path: Path | None) -> None:
    """Migration 002: fill leads.city from the plan of the run that first found each lead."""
    cities = _plan_cities(runs_db_path)
    conn.executemany("UPDATE leads SET city = ? WHERE first_run_id = ? AND city IS NULL",
                     [(city, run_id) for run_id, city in cities.items()])


# Python steps that run right after a numbered SQL file, inside the same transaction.
_PY_STEPS: dict[int, Callable[[sqlite3.Connection, Path | None], None]] = {2: _backfill_lead_city}


class MemoryDB:
    def __init__(self, path: Path | str, *, now: Callable[[], datetime] | None = None,
                 rejected_retry_days: int = 90, ids: Callable[[], str] | None = None) -> None:
        self.path = path
        self._now = now or (lambda: datetime.now(UTC))
        self.rejected_retry_days = rejected_retry_days
        self._ids = ids
        self.conn = connect(path)

    def close(self) -> None:
        self.conn.close()

    def now(self) -> str:
        return self._now().isoformat()

    def _new_id(self) -> str:
        if self._ids:
            return self._ids()
        from ulid import ULID
        return str(ULID())

    # --- migrations ---------------------------------------------------------

    @property
    def user_version(self) -> int:
        return int(self.conn.execute("PRAGMA user_version").fetchone()[0])

    def migrate(self, runs_db_path: Path | None,
                schema_dir: Traversable | Path | None = None) -> list[int]:
        """Applies pending numbered migrations. Returns the versions applied.

        Refuses while a run is unfinished, but only when a migration is pending.
        Backs up an existing database to `memory.db.bak-<version>` first.
        """
        schema_dir = schema_dir or resources.files("outreach.db").joinpath("memory_schema")
        current = self.user_version
        pending = [(v, f) for v, f in _migrations(schema_dir) if v > current]
        if not pending:
            return []
        unfinished = _unfinished_run_ids(runs_db_path)
        if unfinished:
            raise MigrationRefused(
                f"memory.db needs migration {current} -> {pending[-1][0]}, but run(s) "
                f"{', '.join(unfinished)} are unfinished. Finish or discard them first.")
        if current > 0 and str(self.path) != ":memory:":
            backup_path = Path(f"{self.path}.bak-{current}")
            dest = sqlite3.connect(str(backup_path))
            try:
                self.conn.backup(dest)
            finally:
                dest.close()
        applied = []
        # One transaction for every pending file; executescript would commit per file.
        previous_level = self.conn.isolation_level
        self.conn.isolation_level = None
        try:
            self.conn.execute("BEGIN")
            for version, file in pending:
                for statement in _split_sql(file.read_text(encoding="utf-8")):
                    self.conn.execute(statement)
                if version in _PY_STEPS:
                    _PY_STEPS[version](self.conn, runs_db_path)
                applied.append(version)
            self.conn.execute(f"PRAGMA user_version = {pending[-1][0]}")
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        finally:
            self.conn.isolation_level = previous_level
        return applied

    # --- registry -----------------------------------------------------------

    def lead(self, domain_key: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM leads WHERE domain_key = ?", (domain_key,)).fetchone()
        return row

    def skip_reason(self, domain_key: str, run_checklist_key: str | None) -> str | None:
        """Why `should_skip` is true, or None. See the design's Rules section."""
        if self.conn.execute("SELECT 1 FROM do_not_contact WHERE domain_key = ?", (domain_key,)).fetchone():
            return "do_not_contact"
        if self.conn.execute("SELECT 1 FROM contacted WHERE domain_key = ?", (domain_key,)).fetchone():
            return "contacted"
        row = self.lead(domain_key)
        if row is None:
            return None
        status = row["research_status"]
        if status in {s.value for s in REJECTED_STATUSES}:
            updated = datetime.fromisoformat(row["updated_at"])
            if self._now() - updated < timedelta(days=self.rejected_retry_days):
                return "recently_rejected"
        if (status in (ResearchStatus.candidate, ResearchStatus.parked_no_email)
                and run_checklist_key is not None and row["last_checklist_key"] == run_checklist_key):
            return "already_researched"
        return None

    def should_skip(self, domain_key: str, run_checklist_key: str | None) -> bool:
        return self.skip_reason(domain_key, run_checklist_key) is not None

    def claim(self, domain_key: str, run_id: str, run_checklist_key: str | None, city: str | None = None) -> bool:
        """Marks the domain `researching` for this run. Refuses skipped domains.

        `city` is the run plan's city; the first city a lead is found in is kept.
        """
        city = (city or "").strip() or None
        with self.conn:
            if self.should_skip(domain_key, run_checklist_key):
                return False
            self.conn.execute(
                "INSERT INTO leads (domain_key, research_status, first_run_id, last_run_id,"
                " normalization_version, updated_at, city) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(domain_key) DO UPDATE SET research_status = excluded.research_status,"
                " last_run_id = excluded.last_run_id, normalization_version = excluded.normalization_version,"
                " updated_at = excluded.updated_at, city = COALESCE(leads.city, excluded.city)",
                (domain_key, ResearchStatus.researching, run_id, run_id, NORMALIZATION_VERSION, self.now(), city))
        return True

    def finalize(self, domain_key: str, run_id: str, status: ResearchStatus) -> None:
        """Writes the final research status. Called last, after runs.db evidence rows."""
        with self.conn:
            self.conn.execute(
                "UPDATE leads SET research_status = ?, last_run_id = ?, updated_at = ? WHERE domain_key = ?",
                (status, run_id, self.now(), domain_key))

    def release(self, run_id: str) -> int:
        """Releases this run's unfinished claims. Returns how many rows changed."""
        with self.conn:
            cur = self.conn.execute(
                "UPDATE leads SET research_status = ?, updated_at = ? WHERE last_run_id = ?"
                " AND research_status IN (?, ?)",
                (ResearchStatus.released, self.now(), run_id, ResearchStatus.queued,
                 ResearchStatus.researching))
        return cur.rowcount

    def record_qualification(self, domain_key: str, run_checklist_key: str) -> None:
        with self.conn:
            self.conn.execute("UPDATE leads SET last_checklist_key = ?, updated_at = ? WHERE domain_key = ?",
                              (run_checklist_key, self.now(), domain_key))

    def block_many(self, domain_keys: Iterable[str], run_id: str, reason: str = "rejected") -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO do_not_contact (domain_key, reason, run_id, created_at)"
                " VALUES (?, ?, ?, ?)", [(d, reason, run_id, self.now()) for d in domain_keys])

    def is_blocked_or_contacted(self, domain_key: str) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM do_not_contact WHERE domain_key = ? UNION ALL"
            " SELECT 1 FROM contacted WHERE domain_key = ?", (domain_key, domain_key)).fetchone())

    def mark_contacted(self, domain_key: str, run_id: str, draft_id: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO contacted (domain_key, run_id, draft_id, sent_at)"
                              " VALUES (?, ?, ?, ?)", (domain_key, run_id, draft_id, self.now()))

    def list_leads(self, kind: str) -> list[sqlite3.Row]:
        """For `outreach leads --status`: parked, blocked or contacted."""
        if kind == "parked":
            return list(self.conn.execute("SELECT domain_key, last_run_id AS run_id, updated_at AS at, city FROM leads"
                                          " WHERE research_status = ? ORDER BY updated_at",
                                          (ResearchStatus.parked_no_email,)))
        if kind == "blocked":
            return list(self.conn.execute("SELECT d.domain_key, d.run_id, d.created_at AS at, d.reason, l.city"
                                          " FROM do_not_contact d LEFT JOIN leads l USING (domain_key)"
                                          " ORDER BY d.created_at"))
        if kind == "contacted":
            return list(self.conn.execute("SELECT c.domain_key, c.run_id, c.sent_at AS at, c.draft_id, l.city"
                                          " FROM contacted c LEFT JOIN leads l USING (domain_key)"
                                          " ORDER BY c.sent_at"))
        raise ValueError(f"unknown lead list {kind!r}")

    def domains_for_city(self, city: str) -> set[str]:
        """Companies whose recorded city matches (same matching as `city_key`)."""
        want = city_key(city)
        return {r[0] for r in self.conn.execute("SELECT domain_key, city FROM leads WHERE city IS NOT NULL")
                if city_key(r[1]) == want}

    def known_cities(self) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT DISTINCT city FROM leads WHERE city IS NOT NULL ORDER BY city COLLATE NOCASE")]

    def domains_for_runs(self, run_ids: Iterable[str]) -> set[str]:
        """Companies whose registry rows point at any of these runs."""
        ids = list(run_ids)
        if not ids:
            return set()
        marks = ", ".join("?" * len(ids))
        sql = (f"SELECT domain_key FROM leads WHERE first_run_id IN ({marks}) OR last_run_id IN ({marks})"
               f" UNION SELECT domain_key FROM do_not_contact WHERE run_id IN ({marks})"
               f" UNION SELECT domain_key FROM contacted WHERE run_id IN ({marks})")
        return {r[0] for r in self.conn.execute(sql, ids * 4)}

    def purge(self, domain_key: str) -> None:
        """Forgets a company entirely, including any do-not-contact entry."""
        with self.conn:
            for table in ("leads", "do_not_contact", "contacted"):
                self.conn.execute(f"DELETE FROM {table} WHERE domain_key = ?", (domain_key,))

    # --- remembered answers -------------------------------------------------

    def remember_sender(self, name: str, business_name: str, call_to_action: str) -> str:
        profile_id = self._new_id()
        with self.conn:
            self.conn.execute(
                "INSERT INTO sender_profiles (profile_id, name, business_name, call_to_action, remembered,"
                " created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (profile_id, name, business_name, call_to_action, self.now()))
        return profile_id

    def latest_sender(self) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM sender_profiles WHERE remembered = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row

    def remember_checklist(self, items: list[dict], threshold: int | None) -> str:
        key = checklist_key(items, threshold)
        with self.conn:
            self.conn.execute(
                "INSERT INTO checklists (checklist_key, items_json, threshold, remembered, created_at)"
                " VALUES (?, ?, ?, 1, ?) ON CONFLICT(checklist_key) DO UPDATE SET remembered = 1,"
                " created_at = excluded.created_at", (key, json.dumps(items), threshold, self.now()))
        return key

    def latest_checklist(self) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(
            "SELECT * FROM checklists WHERE remembered = 1 ORDER BY created_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row

    def remember_preference(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO preferences (key, value, remembered, updated_at) VALUES (?, ?, 1, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value, remembered = 1,"
                " updated_at = excluded.updated_at", (key, value, self.now()))

    def preference(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM preferences WHERE key = ? AND remembered = 1",
                                (key,)).fetchone()
        return None if row is None else str(row[0])


def _split_sql(script: str) -> list[str]:
    """Splits a migration file on ';' at line ends. Migrations must not use ';' inside literals."""
    lines = [ln for ln in script.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
