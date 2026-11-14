"""Access to runs.db: the LangGraph checkpointer connection plus the run tables.

Every `RunStatus` field is computed here from the tables (`build_run_status`); graph state holds
none of it. Only memory_db.py writes the cross-run registry.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from langgraph.checkpoint.sqlite import SqliteSaver

from outreach.db import city_key, connect
from outreach.models import (
    UNFINISHED_RUN_STATUSES,
    Checklist,
    QuestionKey,
    ResearchStatus,
    RunPlan,
    RunStatus,
    RunStatusValue,
    SenderProfile,
    SendStatus,
    StageStopReason,
)

if TYPE_CHECKING:
    from outreach.config import Settings

_FINAL = "('approve', 'edit', 'reject')"


def text_hash(subject: str, body: str) -> str:
    """The hash the send guard compares: sha256(subject + "\n\n" + body)."""
    return hashlib.sha256(f"{subject}\n\n{body}".encode()).hexdigest()


def _now(clock_now: Callable[[], datetime] | None) -> str:
    from datetime import UTC
    return (clock_now() if clock_now else datetime.now(UTC)).isoformat()


class RunsDB:
    def __init__(self, path: Path | str, *, now: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path) if str(path) != ":memory:" else path
        self._now = now
        self.conn = connect(path)
        self.saver_conn = self.conn if str(path) == ":memory:" else connect(path)
        self.saver = SqliteSaver(self.saver_conn)
        self.saver.setup()
        ddl = resources.files("outreach.db").joinpath("schema_runs.sql").read_text(encoding="utf-8")
        self.conn.executescript(ddl)
        # Test-only hook, called inside set_candidate_status (test_resume_research).
        self.fault_hook: Callable[[str, str, ResearchStatus], None] | None = None

    def now(self) -> str:
        return _now(self._now)

    def close(self) -> None:
        self.conn.close()
        if self.saver_conn is not self.conn:
            self.saver_conn.close()

    def _one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self.conn.execute(sql, tuple(params)).fetchone()
        return row

    def _scalar(self, sql: str, params: Iterable[Any] = ()) -> Any:
        row = self.conn.execute(sql, tuple(params)).fetchone()
        return None if row is None else row[0]

    # --- runs ---------------------------------------------------------------

    def create_run(self, *, run_id: str, app_version: str, router: str, prompt: str,
                   seeds: list[str] | None = None, city_override: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO runs (run_id, created_at, app_version, status, router, prompt, seeds_json,"
                " city_override, plan_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)",
                (run_id, self.now(), app_version, RunStatusValue.active, router, prompt,
                 json.dumps(seeds) if seeds is not None else None, city_override),
            )

    def get_run(self, run_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM runs WHERE run_id = ?", (run_id,))

    def set_run_status(self, run_id: str, status: RunStatusValue, stop_reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET status = ?, stop_reason = COALESCE(?, stop_reason)"
                              " WHERE run_id = ?", (status, stop_reason, run_id))

    def set_parsed(self, run_id: str, parsed: dict) -> None:
        with self.conn:
            self.conn.execute("UPDATE runs SET parsed_json = ? WHERE run_id = ?", (json.dumps(parsed), run_id))

    def unfinished_runs(self) -> list[sqlite3.Row]:
        marks = ",".join("?" * len(UNFINISHED_RUN_STATUSES))
        return list(self.conn.execute(
            f"SELECT * FROM runs WHERE status IN ({marks}) ORDER BY created_at",
            tuple(s.value for s in UNFINISHED_RUN_STATUSES)))

    # --- plans, answers, overrides ------------------------------------------

    def write_plan(self, run_id: str, *, version: int, status: str, city: str | None,
                   target_qualified: int, checklist: dict, sender: dict,
                   checklist_key: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO run_plans (run_id, plan_version, status, city, target_qualified,"
                " checklist_json, checklist_key, sender_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, version, status, city, target_qualified, json.dumps(checklist), checklist_key,
                 json.dumps(sender)),
            )
            self.conn.execute("UPDATE runs SET plan_version = ? WHERE run_id = ?", (version, run_id))

    def latest_plan(self, run_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM run_plans WHERE run_id = ? ORDER BY plan_version DESC LIMIT 1",
                         (run_id,))

    def plan_model(self, run_id: str) -> RunPlan | None:
        """The latest plan version as a `RunPlan`."""
        row = self.latest_plan(run_id)
        if row is None:
            return None
        checklist = json.loads(row["checklist_json"])
        return RunPlan(
            run_id=run_id, version=row["plan_version"], status=row["status"], city=row["city"],
            target_qualified=row["target_qualified"],
            checklist=Checklist(key=row["checklist_key"] or "", version=row["plan_version"], **checklist),
            sender=SenderProfile(**json.loads(row["sender_json"])),
        )

    def write_plan_model(self, plan: RunPlan) -> None:
        self.write_plan(
            plan.run_id, version=plan.version, status=plan.status, city=plan.city,
            target_qualified=plan.target_qualified,
            checklist={"items": [i.model_dump() for i in plan.checklist.items],
                       "threshold": plan.checklist.threshold},
            sender=plan.sender.model_dump(), checklist_key=plan.checklist.key or None)

    def new_plan_version(self, run_id: str) -> int:
        current = self._scalar("SELECT MAX(plan_version) FROM run_plans WHERE run_id = ?", (run_id,))
        return (current or 0) + 1

    def record_answer(self, *, pause_id: str, run_id: str, key: str, value: str, origin: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO answers (pause_id, run_id, key, value, origin, answered_at)"
                " VALUES (?, ?, ?, ?, ?, ?)", (pause_id, run_id, key, value, origin, self.now()))

    def answers(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM answers WHERE run_id = ? ORDER BY answered_at",
                                      (run_id,)))

    def add_override(self, run_id: str, key: str, value: str) -> int:
        with self.conn:
            seq = (self._scalar("SELECT MAX(seq) FROM run_overrides WHERE run_id = ? AND key = ?",
                                (run_id, key)) or 0) + 1
            self.conn.execute("INSERT INTO run_overrides (run_id, key, seq, value) VALUES (?, ?, ?, ?)",
                              (run_id, key, seq, value))
        return int(seq)

    def overrides(self, run_id: str, key: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT value FROM run_overrides WHERE run_id = ? AND key = ? ORDER BY seq", (run_id, key))]

    # --- rounds -------------------------------------------------------------

    def open_round(self, run_id: str, plan_version: int) -> int:
        """Re-enters the open round if there is one, otherwise opens the next."""
        with self.conn:
            open_row = self._one("SELECT round FROM research_rounds WHERE run_id = ? AND closed_at IS NULL"
                                 " ORDER BY round DESC LIMIT 1", (run_id,))
            if open_row is not None:
                return int(open_row["round"])
            nxt = (self._scalar("SELECT MAX(round) FROM research_rounds WHERE run_id = ?", (run_id,)) or 0) + 1
            self.conn.execute("INSERT INTO research_rounds (run_id, round, plan_version, started_at)"
                              " VALUES (?, ?, ?, ?)", (run_id, nxt, plan_version, self.now()))
            return int(nxt)

    def close_round(self, run_id: str, round_: int, *, new_companies: int, stop_reason: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE research_rounds SET closed_at = ?, new_companies = ?, stop_reason = ?"
                " WHERE run_id = ? AND round = ? AND closed_at IS NULL",
                (self.now(), new_companies, stop_reason, run_id, round_))

    def close_open_round(self, run_id: str) -> int | None:
        """finalize: closes a round left open, counting its counted companies. Returns the round."""
        row = self._one("SELECT round FROM research_rounds WHERE run_id = ? AND closed_at IS NULL", (run_id,))
        if row is None:
            return None
        counted = self._scalar("SELECT COUNT(*) FROM run_candidates WHERE run_id = ? AND round = ? AND counted = 1",
                               (run_id, row["round"]))
        self.close_round(run_id, row["round"], new_companies=int(counted), stop_reason=None)
        return int(row["round"])

    def end_screen_counts(self, run_id: str) -> dict[str, Any]:
        """The funnel, drop reasons, usage and assumptions for the end screen."""
        one = self._scalar
        approved = sum(1 for d in self.drafts(run_id)
                       if (fr := self.final_review(d["draft_id"])) is not None and fr["action"] != "reject")
        sends = {r[0]: r[1] for r in self.conn.execute(
            "SELECT status, COUNT(*) FROM send_log WHERE run_id = ? GROUP BY status", (run_id,))}
        drops: dict[str, int] = {}
        for sql in ("SELECT drop_reason, COUNT(*) FROM run_candidates WHERE run_id = ? AND drop_reason IS NOT NULL"
                    " GROUP BY drop_reason",
                    "SELECT drop_reason, COUNT(*) FROM qualifications WHERE run_id = ? AND drop_reason IS NOT NULL"
                    " GROUP BY drop_reason"):
            for reason, n in self.conn.execute(sql, (run_id,)):
                drops[reason] = drops.get(reason, 0) + n
        return {
            "funnel": {
                "found": one("SELECT COUNT(*) FROM run_candidates WHERE run_id = ?", (run_id,)),
                "counted": one("SELECT COUNT(*) FROM run_candidates WHERE run_id = ? AND counted = 1", (run_id,)),
                "qualified": one("SELECT COUNT(*) FROM qualifications WHERE run_id = ? AND qualified = 1", (run_id,)),
                "drafted": one("SELECT COUNT(*) FROM drafts WHERE run_id = ? AND status = 'ok'", (run_id,)),
                "approved": approved,
                "sent": sends.get(SendStatus.sent, 0),
                "blocked": sends.get(SendStatus.blocked, 0),
                "failed": sends.get(SendStatus.failed, 0),
                "unknown": sends.get(SendStatus.unknown, 0),
            },
            "drop_reasons": drops,
            "usage": {
                "searches": one("SELECT COUNT(*) FROM api_calls WHERE run_id = ? AND vendor = 'exa'", (run_id,)),
                "pages": one("SELECT COUNT(*) FROM pages WHERE run_id = ?", (run_id,)),
                "tokens": one("SELECT COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0)"
                              " FROM api_calls WHERE run_id = ? AND vendor = 'anthropic'", (run_id,)),
                "hunter_credits": one("SELECT COALESCE(SUM(units), 0) FROM api_calls WHERE run_id = ?"
                                      " AND vendor = 'hunter'", (run_id,)),
                "cost_estimate": one("SELECT COALESCE(SUM(cost_estimate), 0) FROM api_calls WHERE run_id = ?",
                                     (run_id,)),
            },
            "assumptions": [r[0] for r in self.conn.execute(
                "SELECT assumption FROM supervisor_decisions WHERE run_id = ? AND assumption IS NOT NULL"
                " ORDER BY turn", (run_id,))],
            "blocked_sends": [dict(r) for r in self.conn.execute(
                "SELECT draft_id, domain_key, status, reason FROM send_log WHERE run_id = ? AND status != 'sent'",
                (run_id,))],
        }

    # --- caches -------------------------------------------------------------

    def cache_search_get(self, request_hash: str) -> dict | None:
        raw = self._scalar("SELECT response_json FROM search_calls WHERE request_hash = ?", (request_hash,))
        return None if raw is None else json.loads(raw)

    def cache_search_put(self, *, request_hash: str, run_id: str, round_: int, query: str,
                         response: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO search_calls (request_hash, run_id, round, query, response_json,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (request_hash, run_id, round_, query, json.dumps(response), self.now()))

    def page_get(self, run_id: str, requested_url: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM pages WHERE run_id = ? AND requested_url = ?", (run_id, requested_url))

    def page_put(self, *, run_id: str, lead_id: str, requested_url: str, fetch_status: str,
                 final_url: str | None = None, http_status: int | None = None,
                 content_type: str | None = None, raw_html_zlib: bytes | None = None,
                 clean_text: str | None = None, sha256: str | None = None,
                 markers: dict | None = None) -> int:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO pages (run_id, lead_id, requested_url, final_url, http_status,"
                " fetch_status, content_type, raw_html_zlib, clean_text, sha256, markers_json, fetched_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, lead_id, requested_url, final_url, http_status, fetch_status, content_type,
                 raw_html_zlib, clean_text, sha256, json.dumps(markers) if markers else None, self.now()))
        return int(self._scalar("SELECT page_id FROM pages WHERE run_id = ? AND requested_url = ?",
                                (run_id, requested_url)))

    def llm_cache_get(self, key: str) -> str | None:
        value: str | None = self._scalar("SELECT response_json FROM llm_cache WHERE key = ?", (key,))
        return value

    def llm_cache_put(self, key: str, response_json: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO llm_cache (key, response_json, created_at)"
                              " VALUES (?, ?, ?)", (key, response_json, self.now()))

    def hunter_cache_get(self, domain_key: str) -> dict | None:
        raw = self._scalar("SELECT response_json FROM hunter_cache WHERE domain_key = ?", (domain_key,))
        return None if raw is None else json.loads(raw)

    def hunter_cache_put(self, domain_key: str, response: dict) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO hunter_cache (domain_key, response_json, fetched_at)"
                              " VALUES (?, ?, ?)", (domain_key, json.dumps(response), self.now()))

    # --- candidates and qualifications -------------------------------------

    def queue_candidates(self, run_id: str, round_: int, lead_ids: Iterable[str],
                         found_by_query: str | None = None) -> list[str]:
        """INSERT OR IGNORE; returns the lead_ids that were new to this run."""
        new: list[str] = []
        with self.conn:
            pos = self._scalar("SELECT COALESCE(MAX(queue_pos), 0) FROM run_candidates WHERE run_id = ?",
                               (run_id,)) or 0
            for lead_id in lead_ids:
                pos += 1
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO run_candidates (run_id, lead_id, round, queue_pos, status,"
                    " found_by_query) VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, lead_id, round_, pos, ResearchStatus.queued, found_by_query))
                if cur.rowcount:
                    new.append(lead_id)
        return new

    def next_queued(self, run_id: str) -> sqlite3.Row | None:
        """Unfinished rows first (`researching` after a crash), then the queue in order."""
        return self._one(
            "SELECT * FROM run_candidates WHERE run_id = ? AND status IN (?, ?)"
            " ORDER BY CASE status WHEN ? THEN 0 ELSE 1 END, queue_pos LIMIT 1",
            (run_id, ResearchStatus.researching, ResearchStatus.queued, ResearchStatus.researching))

    def set_candidate_status(self, run_id: str, lead_id: str, status: ResearchStatus, *,
                             drop_reason: str | None = None, judge_json: str | None = None,
                             us_evidence: str | None = None) -> None:
        if self.fault_hook is not None:
            self.fault_hook(run_id, lead_id, status)
        counted = 1 if status in (ResearchStatus.candidate, ResearchStatus.parked_no_email) else 0
        with self.conn:
            self.conn.execute(
                "UPDATE run_candidates SET status = ?, counted = ?, drop_reason = COALESCE(?, drop_reason),"
                " judge_json = COALESCE(?, judge_json), us_evidence = COALESCE(?, us_evidence)"
                " WHERE run_id = ? AND lead_id = ?",
                (status, counted, drop_reason, judge_json, us_evidence, run_id, lead_id))

    def release_open_candidates(self, run_id: str) -> list[str]:
        """Marks rows still `queued`/`researching` as `released` (finalize, discard)."""
        with self.conn:
            rows = [r[0] for r in self.conn.execute(
                "SELECT lead_id FROM run_candidates WHERE run_id = ? AND status IN (?, ?)",
                (run_id, ResearchStatus.queued, ResearchStatus.researching))]
            self.conn.execute(
                "UPDATE run_candidates SET status = ? WHERE run_id = ? AND status IN (?, ?)",
                (ResearchStatus.released, run_id, ResearchStatus.queued, ResearchStatus.researching))
        return rows

    def write_qualification(self, *, run_id: str, lead_id: str, checklist_version: int, points: int,
                            verdicts_json: str, qualified: bool, email: str | None = None,
                            email_type: str | None = None, email_source_kind: str | None = None,
                            email_source_url: str | None = None, greeting_name: str | None = None,
                            drop_reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO qualifications (run_id, lead_id, checklist_version, points,"
                " verdicts_json, qualified, email, email_type, email_source_kind, email_source_url,"
                " greeting_name, drop_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, lead_id, checklist_version, points, verdicts_json, int(qualified), email,
                 email_type, email_source_kind, email_source_url, greeting_name, drop_reason))

    def delete_qualifications(self, run_id: str, lead_ids: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany("DELETE FROM qualifications WHERE run_id = ? AND lead_id = ?",
                                  [(run_id, lid) for lid in lead_ids])

    def qualification(self, run_id: str, lead_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM qualifications WHERE run_id = ? AND lead_id = ?", (run_id, lead_id))

    def rescore(self, run_id: str, threshold: int, checklist_version: int) -> list[str]:
        """Recomputes `qualified` for a new threshold, with no model call.

        Leads that now pass but have no email lose their row, so `qualify` runs Hunter for them
        (their verdicts come back from llm_cache). Returns those lead ids.
        """
        with self.conn:
            requalify = [r[0] for r in self.conn.execute(
                "SELECT lead_id FROM qualifications WHERE run_id = ? AND points >= ? AND email IS NULL",
                (run_id, threshold))]
            self.conn.executemany("DELETE FROM qualifications WHERE run_id = ? AND lead_id = ?",
                                  [(run_id, lid) for lid in requalify])
            self.conn.execute(
                "UPDATE qualifications SET qualified = (points >= ? AND email IS NOT NULL),"
                " checklist_version = ? WHERE run_id = ?", (threshold, checklist_version, run_id))
        return requalify

    def evidence(self, run_id: str, lead_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM evidence WHERE run_id = ? AND lead_id = ? ORDER BY rowid",
                                      (run_id, lead_id)))

    def count_candidates(self, run_id: str, *statuses: ResearchStatus) -> int:
        marks = ",".join("?" * len(statuses))
        return int(self._scalar(f"SELECT COUNT(*) FROM run_candidates WHERE run_id = ? AND status IN ({marks})",
                                (run_id, *statuses)))

    def add_evidence(self, *, evidence_id: str, run_id: str, lead_id: str, kind: str, quote: str,
                     page_id: int | None = None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO evidence (evidence_id, run_id, lead_id, page_id, kind, quote)"
                              " VALUES (?, ?, ?, ?, ?, ?)", (evidence_id, run_id, lead_id, page_id, kind, quote))

    # --- researcher (phase 4) ---------------------------------------------------

    def triage_get(self, domain_key: str, prompt_version: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM triage_labels WHERE domain_key = ? AND prompt_version = ?",
                         (domain_key, prompt_version))

    def triage_put(self, *, domain_key: str, label: str, confidence: float | None, reason: str | None,
                   prompt_version: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO triage_labels (domain_key, label, confidence, reason,"
                              " prompt_version) VALUES (?, ?, ?, ?, ?)",
                              (domain_key, label, confidence, reason, prompt_version))

    def round_has_rows(self, run_id: str, round_: int) -> bool:
        return self._one("SELECT 1 FROM run_candidates WHERE run_id = ? AND round = ? LIMIT 1",
                         (run_id, round_)) is not None

    def round_rows(self, run_id: str, round_: int, status: ResearchStatus | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM run_candidates WHERE run_id = ? AND round = ?"
        params: list[Any] = [run_id, round_]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        return list(self.conn.execute(sql + " ORDER BY queue_pos", params))

    def round_counted(self, run_id: str, round_: int) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM run_candidates WHERE run_id = ? AND round = ? AND counted = 1",
                                (run_id, round_)))

    def move_to_round(self, run_id: str, lead_id: str, round_: int) -> None:
        """A queued row carried over from an earlier round is researched, and counted, in this one."""
        with self.conn:
            self.conn.execute("UPDATE run_candidates SET round = ? WHERE run_id = ? AND lead_id = ?",
                              (round_, run_id, lead_id))

    def searches_in_round(self, run_id: str, round_: int) -> int:
        return int(self._scalar("SELECT COUNT(*) FROM search_calls WHERE run_id = ? AND round = ?",
                                (run_id, round_)))

    def search_responses(self, run_id: str, round_: int) -> list[tuple[str, dict]]:
        return [(r["query"], json.loads(r["response_json"])) for r in self.conn.execute(
            "SELECT query, response_json FROM search_calls WHERE run_id = ? AND round = ? ORDER BY rowid",
            (run_id, round_))]

    def queries_tried(self, run_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT query FROM search_calls WHERE run_id = ? ORDER BY rowid", (run_id,))]

    def set_page_markers(self, page_id: int, markers: list[str]) -> None:
        with self.conn:
            self.conn.execute("UPDATE pages SET markers_json = ? WHERE page_id = ?", (json.dumps(markers), page_id))

    def pages_for_lead(self, run_id: str, lead_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM pages WHERE run_id = ? AND lead_id = ? ORDER BY page_id",
                                      (run_id, lead_id)))

    def add_email_observation(self, *, obs_id: str, run_id: str, lead_id: str, address_norm: str,
                              address_as_seen: str, page_id: int | None, source_url: str, method: str,
                              domain_relation: str, hunter_json: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO email_observations (obs_id, run_id, lead_id, address_norm, address_as_seen,"
                " page_id, source_url, method, domain_relation, hunter_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (obs_id, run_id, lead_id, address_norm, address_as_seen, page_id, source_url, method,
                 domain_relation, hunter_json))

    def email_observations(self, run_id: str, lead_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM email_observations WHERE run_id = ? AND lead_id = ? ORDER BY rowid", (run_id, lead_id)))

    def add_phone(self, run_id: str, lead_id: str, e164: str, page_id: int | None) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO phones_seen (run_id, lead_id, e164, page_id) VALUES (?, ?, ?, ?)",
                              (run_id, lead_id, e164, page_id))

    def phones(self, run_id: str, lead_id: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT e164 FROM phones_seen WHERE run_id = ? AND lead_id = ? ORDER BY rowid", (run_id, lead_id))]

    def candidate(self, run_id: str, lead_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM run_candidates WHERE run_id = ? AND lead_id = ?", (run_id, lead_id))

    def judge_facts(self, run_id: str, lead_id: str) -> dict[str, Any]:
        """The verified judge result the researcher stored (`judge_json` is {"judge": {...}, ...}); {} if none.
        The one reader of that shape, so the qualifier, drafter and review screen can't disagree on it."""
        row = self.candidate(run_id, lead_id)
        stored = json.loads(row["judge_json"]) if row is not None and row["judge_json"] else None
        judge = stored.get("judge") if isinstance(stored, dict) else None
        return judge if isinstance(judge, dict) else {}

    # --- run status ---------------------------------------------------------

    def build_run_status(self, run_id: str, settings: Settings,
                         is_blocked: Callable[[str], bool] = lambda _d: False) -> RunStatus:
        """Every field from the tables (design: Supervisor contract). `is_blocked` checks memory.db."""
        run = self.get_run(run_id)
        router = run["router"] if run is not None else settings.default_router
        plan = self.plan_model(run_id)
        target = plan.target_qualified if plan else settings.target_qualified
        threshold = plan.checklist.threshold if plan else None
        unqualified = self._scalar(
            "SELECT COUNT(*) FROM run_candidates c WHERE c.run_id = ? AND c.status IN (?, ?) AND NOT EXISTS"
            " (SELECT 1 FROM qualifications q WHERE q.run_id = c.run_id AND q.lead_id = c.lead_id)",
            (run_id, ResearchStatus.candidate, ResearchStatus.parked_no_email))
        qualified = self._scalar("SELECT COUNT(*) FROM qualifications WHERE run_id = ? AND qualified = 1",
                                 (run_id,))
        near_miss = 0
        if threshold is not None and threshold >= 2:
            near_miss = self._scalar("SELECT COUNT(*) FROM qualifications WHERE run_id = ? AND points = ?",
                                     (run_id, threshold - 1))
        rounds_used = self._scalar("SELECT COUNT(*) FROM research_rounds WHERE run_id = ?", (run_id,))
        max_rounds = settings.max_research_rounds + len(self.overrides(run_id, "extra_round"))
        last_new = None
        if plan is not None:
            last_new = self._scalar(
                "SELECT new_companies FROM research_rounds WHERE run_id = ? AND plan_version = ?"
                " AND closed_at IS NOT NULL ORDER BY round DESC LIMIT 1", (run_id, plan.version))
        last_stop = self._scalar("SELECT stop_reason FROM research_rounds WHERE run_id = ?"
                                 " AND closed_at IS NOT NULL ORDER BY round DESC LIMIT 1", (run_id,))
        questions_used = self._scalar(
            "SELECT COUNT(*) FROM answers WHERE run_id = ? AND origin = 'supervisor'", (run_id,))
        decisions_used = self._scalar(
            "SELECT COUNT(*) FROM supervisor_decisions WHERE run_id = ? AND decided_by IN ('router', 'fallback')",
            (run_id,))
        answered = frozenset(QuestionKey(r[0]) for r in self.conn.execute(
            "SELECT DISTINCT key FROM answers WHERE run_id = ?", (run_id,)) if r[0] in QuestionKey.__members__)
        exhausted: Literal["rounds", "empty_round"] | None = None
        if rounds_used >= max_rounds:
            exhausted = "rounds"
        elif last_new == 0:
            exhausted = "empty_round"
        available: set[QuestionKey] = set()
        if exhausted is not None and qualified < target:
            if exhausted == "rounds":
                available.add(QuestionKey.extra_round)
            available.add(QuestionKey.change_city)
            if near_miss > 0:
                available.add(QuestionKey.lower_threshold)
        return RunStatus(
            unqualified_candidates=unqualified, qualified_count=qualified, near_miss_leads=near_miss,
            target_qualified=target, research_rounds_used=rounds_used, max_research_rounds=max_rounds,
            last_round_new_companies=last_new, undrafted_qualified=len(self.draftable_leads(run_id, is_blocked)),
            questions_used=questions_used, max_questions=settings.max_questions, decisions_used=decisions_used,
            max_decisions=settings.max_decisions, answered_keys=answered,
            available_question_keys=frozenset(available - answered), exhausted_reason=exhausted,
            last_stage_stop_reason=StageStopReason(last_stop) if last_stop else None,
            router=cast(Literal["claude", "code"], router),
        )

    # --- drafts and review --------------------------------------------------

    def draftable_leads(self, run_id: str, is_blocked: Callable[[str], bool] = lambda _d: False) -> list[str]:
        """Qualified leads with no draft row (ok or draft_failed) that aren't on do-not-contact/contacted."""
        rows = self.conn.execute(
            "SELECT q.lead_id FROM qualifications q LEFT JOIN run_candidates c"
            " ON c.run_id = q.run_id AND c.lead_id = q.lead_id"
            " WHERE q.run_id = ? AND q.qualified = 1 AND NOT EXISTS"
            " (SELECT 1 FROM drafts d WHERE d.run_id = q.run_id AND d.lead_id = q.lead_id)"
            " ORDER BY COALESCE(c.queue_pos, 0), q.lead_id", (run_id,))
        return [r[0] for r in rows if not is_blocked(r[0])]

    def insert_draft(self, *, draft_id: str, run_id: str, lead_id: str, subject: str, body: str,
                     claim_flags: list[str], model: str | None, prompt_version: str | None,
                     status: str = "ok") -> None:
        """queue_pos follows insertion order."""
        with self.conn:
            pos = (self._scalar("SELECT MAX(queue_pos) FROM drafts WHERE run_id = ?", (run_id,)) or 0) + 1
            self.conn.execute(
                "INSERT OR IGNORE INTO drafts (draft_id, run_id, lead_id, subject, body, text_hash,"
                " claim_flags_json, model, prompt_version, status, queue_pos)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (draft_id, run_id, lead_id, subject, body, text_hash(subject, body), json.dumps(claim_flags),
                 model, prompt_version, status, pos))

    def draft(self, draft_id: str) -> sqlite3.Row | None:
        return self._one("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,))

    def drafts(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM drafts WHERE run_id = ? ORDER BY queue_pos", (run_id,)))

    def final_review(self, draft_id: str) -> sqlite3.Row | None:
        """The latest approve/edit/reject for a draft that `back` hasn't superseded."""
        return self._one(
            f"SELECT * FROM reviews WHERE draft_id = ? AND superseded = 0 AND action IN {_FINAL}"
            " ORDER BY review_seq DESC LIMIT 1", (draft_id,))

    def next_review_item(self, run_id: str) -> sqlite3.Row | None:
        """The ok draft with the lowest queue_pos that has no final decision."""
        return self._one(
            "SELECT * FROM drafts d WHERE d.run_id = ? AND d.status = 'ok' AND NOT EXISTS"
            " (SELECT 1 FROM reviews r WHERE r.draft_id = d.draft_id AND r.superseded = 0"
            f" AND r.action IN {_FINAL}) ORDER BY d.queue_pos, d.draft_id LIMIT 1", (run_id,))

    def review_progress(self, run_id: str) -> tuple[int, int]:
        """(ok drafts with a final decision, ok drafts)."""
        total = self._scalar("SELECT COUNT(*) FROM drafts WHERE run_id = ? AND status = 'ok'", (run_id,))
        decided = self._scalar(
            "SELECT COUNT(*) FROM drafts d WHERE d.run_id = ? AND d.status = 'ok' AND EXISTS"
            " (SELECT 1 FROM reviews r WHERE r.draft_id = d.draft_id AND r.superseded = 0"
            f" AND r.action IN {_FINAL})", (run_id,))
        return int(decided), int(total)

    def next_review_seq(self, draft_id: str) -> int:
        return int(self._scalar("SELECT COALESCE(MAX(review_seq), 0) FROM reviews WHERE draft_id = ?",
                                (draft_id,))) + 1

    def record_review(self, draft_id: str, action: str, *, final_subject: str | None = None,
                      final_body: str | None = None, approved_hash: str | None = None) -> int:
        with self.conn:
            seq = self.next_review_seq(draft_id)
            self.conn.execute(
                "INSERT INTO reviews (draft_id, review_seq, action, final_subject, final_body, approved_hash,"
                " superseded, decided_at) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                (draft_id, seq, action, final_subject, final_body, approved_hash, self.now()))
        return seq

    def skip(self, draft_id: str) -> None:
        """Not a decision: records the skip and moves the draft to the end of the queue."""
        self.record_review(draft_id, "skip")
        with self.conn:
            self.conn.execute(
                "UPDATE drafts SET queue_pos = (SELECT MAX(queue_pos) FROM drafts d2"
                " WHERE d2.run_id = drafts.run_id) + 1 WHERE draft_id = ?", (draft_id,))

    def back(self, run_id: str, current_draft_id: str) -> str | None:
        """Reopens the most recent final decision that isn't superseded; skips are passed over.

        The reopened draft moves to the front of the queue. Returns its id, or None on the first draft.
        """
        self.record_review(current_draft_id, "back")
        with self.conn:
            last = self._one(
                "SELECT r.draft_id, r.review_seq FROM reviews r JOIN drafts d ON d.draft_id = r.draft_id"
                f" WHERE d.run_id = ? AND r.superseded = 0 AND r.action IN {_FINAL}"
                " ORDER BY r.rowid DESC LIMIT 1", (run_id,))
            if last is None:
                return None
            self.conn.execute("UPDATE reviews SET superseded = 1 WHERE draft_id = ? AND review_seq = ?",
                              (last["draft_id"], last["review_seq"]))
            self.conn.execute(
                "UPDATE drafts SET queue_pos = (SELECT MIN(queue_pos) FROM drafts d2"
                " WHERE d2.run_id = drafts.run_id) - 1 WHERE draft_id = ?", (last["draft_id"],))
        return str(last["draft_id"])

    def final_rejects(self, run_id: str) -> list[str]:
        """Lead ids whose final review is a reject."""
        return [d["lead_id"] for d in self.drafts(run_id)
                if (fr := self.final_review(d["draft_id"])) is not None and fr["action"] == "reject"]

    # --- sending ------------------------------------------------------------

    def next_sendable(self, run_id: str) -> sqlite3.Row | None:
        """The next ok draft with no send_log row whose final review is not a reject."""
        return self._one(
            "SELECT * FROM drafts d WHERE d.run_id = ? AND d.status = 'ok'"
            " AND NOT EXISTS (SELECT 1 FROM send_log s WHERE s.draft_id = d.draft_id)"
            " AND COALESCE((SELECT r.action FROM reviews r WHERE r.draft_id = d.draft_id AND r.superseded = 0"
            f" AND r.action IN {_FINAL} ORDER BY r.review_seq DESC LIMIT 1), '') != 'reject'"
            " ORDER BY d.queue_pos, d.draft_id LIMIT 1", (run_id,))

    def send_begin(self, draft_id: str, run_id: str, domain_key: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO send_log (draft_id, run_id, domain_key, status, started_at) VALUES (?, ?, ?, ?, ?)",
                (draft_id, run_id, domain_key, SendStatus.sending, self.now()))

    def send_complete(self, draft_id: str, status: SendStatus, *, message_id: str | None = None,
                      reason: str | None = None) -> None:
        with self.conn:
            self.conn.execute("UPDATE send_log SET status = ?, message_id = ?, reason = ?, finished_at = ?"
                              " WHERE draft_id = ?", (status, message_id, reason, self.now(), draft_id))

    def send_block(self, draft_id: str, run_id: str, domain_key: str, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO send_log (draft_id, run_id, domain_key, status, reason, started_at, finished_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (draft_id, run_id, domain_key, SendStatus.blocked, reason, self.now(), self.now()))

    def mark_sending_unknown(self, run_id: str | None = None) -> int:
        """On resume: a `sending` row may or may not have reached SMTP, so it is never retried."""
        sql = "UPDATE send_log SET status = ?, finished_at = ? WHERE status = ?"
        params: list[Any] = [SendStatus.unknown, self.now(), SendStatus.sending]
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        with self.conn:
            return self.conn.execute(sql, params).rowcount

    def send_log(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM send_log WHERE run_id = ? ORDER BY started_at, draft_id",
                                      (run_id,)))

    def last_send_finished_at(self) -> datetime | None:
        """The latest finished SMTP attempt in any run, so mailer pacing survives a resume."""
        raw = self._scalar("SELECT MAX(finished_at) FROM send_log WHERE status IN (?, ?)",
                           (SendStatus.sent, SendStatus.failed))
        return None if raw is None else datetime.fromisoformat(raw)

    # --- supervisor and accounting -----------------------------------------

    def decisions(self, run_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM supervisor_decisions WHERE run_id = ? ORDER BY turn",
                                      (run_id,)))

    def record_decision(self, *, run_id: str, status_json: str, legal_moves: list[str], router: str,
                        decided_by: str, chosen_move: str, shadow_move: str | None = None,
                        reason: str | None = None, question_key: str | None = None,
                        assumption: str | None = None) -> bool:
        """Skips the write when the last row has the same status_json (a resumed turn). Returns True if written."""
        with self.conn:
            last = self._one("SELECT status_json FROM supervisor_decisions WHERE run_id = ?"
                             " ORDER BY turn DESC LIMIT 1", (run_id,))
            if last is not None and last["status_json"] == status_json:
                return False
            turn = (self._scalar("SELECT COUNT(*) FROM supervisor_decisions WHERE run_id = ?",
                                 (run_id,)) or 0) + 1
            self.conn.execute(
                "INSERT INTO supervisor_decisions (run_id, turn, status_json, legal_moves, router,"
                " decided_by, chosen_move, shadow_move, reason, question_key, assumption)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, turn, status_json, json.dumps(legal_moves), router, decided_by, chosen_move,
                 shadow_move, reason, question_key, assumption))
        return True

    def record_api_call(self, *, call_id: str, run_id: str | None, vendor: str, call_site: str,
                        model: str | None = None, input_tokens: int | None = None,
                        output_tokens: int | None = None, cached_tokens: int | None = None,
                        units: float | None = None, stop_reason: str | None = None,
                        cost_estimate: float | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO api_calls (call_id, run_id, vendor, call_site, model, input_tokens,"
                " output_tokens, cached_tokens, units, stop_reason, cost_estimate, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (call_id, run_id, vendor, call_site, model, input_tokens, output_tokens, cached_tokens,
                 units, stop_reason, cost_estimate, self.now()))

    def api_calls(self, run_id: str | None = None, call_site: str | None = None) -> list[sqlite3.Row]:
        sql, params = "SELECT * FROM api_calls WHERE 1=1", []
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        if call_site is not None:
            sql += " AND call_site = ?"
            params.append(call_site)
        return list(self.conn.execute(sql + " ORDER BY created_at, call_id", params))

    # --- purge --------------------------------------------------------------

    def _latest_plan_cities(self) -> list[tuple[str, str]]:
        """(run_id, city) from each run's latest plan, skipping runs with no city."""
        rows = self.conn.execute(
            "SELECT p.run_id, p.city FROM run_plans p"
            " JOIN (SELECT run_id, MAX(plan_version) AS v FROM run_plans GROUP BY run_id) m"
            " ON p.run_id = m.run_id AND p.plan_version = m.v WHERE p.city IS NOT NULL AND p.city != ''")
        return [(r[0], r[1]) for r in rows]

    def known_cities(self) -> list[str]:
        """Distinct cities that some run's latest plan targeted, as first written."""
        seen: dict[str, str] = {}
        for _, city in self._latest_plan_cities():
            seen.setdefault(city_key(city), city.strip())
        return sorted(seen.values(), key=str.lower)

    def runs_for_city(self, city: str) -> list[str]:
        """Runs whose latest plan targeted `city` (case-insensitive; "Tucson" matches "Tucson, AZ")."""
        want = city_key(city)
        return [run_id for run_id, c in self._latest_plan_cities() if city_key(c) == want]

    def candidate_domains(self, run_ids: Iterable[str]) -> set[str]:
        """Every company that was a candidate in any of these runs."""
        ids = list(run_ids)
        if not ids:
            return set()
        marks = ", ".join("?" * len(ids))
        return {r[0] for r in self.conn.execute(
            f"SELECT DISTINCT lead_id FROM run_candidates WHERE run_id IN ({marks})", ids)}

    def purge_domain(self, domain_key: str) -> None:
        """Deletes every row for one company (used by `outreach purge`)."""
        with self.conn:
            for table in ("run_candidates", "pages", "evidence", "email_observations", "phones_seen",
                          "qualifications"):
                self.conn.execute(f"DELETE FROM {table} WHERE lead_id = ?", (domain_key,))
            draft_ids = [r[0] for r in self.conn.execute("SELECT draft_id FROM drafts WHERE lead_id = ?",
                                                         (domain_key,))]
            self.conn.executemany("DELETE FROM reviews WHERE draft_id = ?", [(d,) for d in draft_ids])
            self.conn.execute("DELETE FROM drafts WHERE lead_id = ?", (domain_key,))
            self.conn.execute("DELETE FROM send_log WHERE domain_key = ?", (domain_key,))
            for table in ("triage_labels", "hunter_cache"):
                self.conn.execute(f"DELETE FROM {table} WHERE domain_key = ?", (domain_key,))
