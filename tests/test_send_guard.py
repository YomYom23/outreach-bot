"""Phase 3 exit test (definition of done): every send-guard check, in order, whatever led to send_next."""

from __future__ import annotations

import json
from typing import Any

import pytest

from outreach import __version__
from outreach.db.runs_db import text_hash
from outreach.deps import Deps
from outreach.graph.outbox import make_send_next
from outreach.models import PlanAnswer, SendStatus
from tests.conftest import make_settings
from tests.helpers import Driver, make_graph

LEAD = "acme-roofing.test"


def seed_draft(deps: Deps, *, draft_id: str = "d1", lead_id: str = LEAD, subject: str = "Hello",
               body: str = "Body") -> None:
    db = deps.runs_db
    db.write_qualification(run_id="R1", lead_id=lead_id, checklist_version=1, points=3,
                           verdicts_json=json.dumps([]), qualified=True, email=f"owner@{lead_id}",
                           email_type="owner", email_source_kind="page")
    db.insert_draft(draft_id=draft_id, run_id="R1", lead_id=lead_id, subject=subject, body=body, claim_flags=[],
                    model="m", prompt_version="v")


def approve(deps: Deps, draft_id: str = "d1", subject: str = "Hello", body: str = "Body") -> None:
    deps.runs_db.record_review(draft_id, "approve", final_subject=subject, final_body=body,
                               approved_hash=text_hash(subject, body))


@pytest.fixture
def run(deps_fake: Deps) -> Deps:
    deps_fake.runs_db.create_run(run_id="R1", app_version=__version__, router="code", prompt="p")
    return deps_fake


def send_all(deps: Deps) -> list[dict[str, Any]]:
    node = make_send_next(deps)
    while node({"run_id": "R1"}).goto == "send_next":
        pass
    return [dict(r) for r in deps.runs_db.send_log("R1")]


def test_unreviewed_draft_reaching_send_next_is_blocked(deps_fake: Deps) -> None:
    """build_graph(test_hooks=True) jumps from confirm_plan straight to send_next."""
    graph, _ = make_graph(deps_fake, [], test_hooks=True)
    d = Driver(graph, deps_fake)
    d.create_run()
    p = d.start()
    while p is not None and p.kind == "question":
        p = d.question(p, {"sender_name": "Sam", "business_name": "Lee", "call_to_action": "reply"}[p.context["key"]])
    seed_draft(deps_fake)  # a draft exists, but nobody reviewed it
    assert p is not None
    p = d.answer(PlanAnswer(pause_id=p.pause_id, action="edit", edits={"threshold": "1"}))
    assert p is not None
    end = d.answer(PlanAnswer(pause_id=p.pause_id, action="confirm"))
    assert end is None  # supervisor -> finish after the send loop drained
    assert deps_fake.mailer.sent == []
    [row] = deps_fake.runs_db.send_log("R1")
    assert (row["status"], row["reason"]) == ("blocked", "not_approved")
    assert not deps_fake.memory_db.is_blocked_or_contacted(LEAD)


def test_check1_reject_is_never_selected_and_unapproved_is_blocked(run: Deps) -> None:
    seed_draft(run, draft_id="d1")
    seed_draft(run, draft_id="d2", lead_id="best-roof.test")
    run.runs_db.record_review("d1", "reject")
    run.runs_db.record_review("d2", "approve", final_subject="Hello", final_body="Body",
                              approved_hash=text_hash("Hello", "Body"))
    run.runs_db.back("R1", "d2")  # supersedes d2's approval
    log = send_all(run)
    assert [(r["draft_id"], r["status"], r["reason"]) for r in log] == [("d2", "blocked", "not_approved")]
    assert run.mailer.sent == []


def test_check2_hash_mismatch_is_blocked(run: Deps) -> None:
    seed_draft(run)
    approve(run)
    run.runs_db.conn.execute("UPDATE reviews SET final_body = 'Body, quietly changed' WHERE draft_id = 'd1'")
    run.runs_db.conn.commit()
    [row] = send_all(run)
    assert (row["status"], row["reason"]) == ("blocked", "hash_mismatch")
    assert run.mailer.sent == []


def test_check3_contacted_or_blocked_domain(run: Deps) -> None:
    seed_draft(run, draft_id="d1")
    seed_draft(run, draft_id="d2", lead_id="best-roof.test")
    approve(run, "d1")
    approve(run, "d2")
    run.memory_db.mark_contacted(LEAD, "R0", "old-draft")
    run.memory_db.block_many(["best-roof.test"], "R0")
    log = send_all(run)
    assert [(r["status"], r["reason"]) for r in log] == [("blocked", "contacted_or_blocked")] * 2
    assert run.mailer.sent == []


def test_check5_host_off_allowlist(run: Deps, tmp_path: Any) -> None:
    run.settings = make_settings(tmp_path, mail_smtp_host="smtp.gmail.com")
    seed_draft(run)
    approve(run)
    [row] = send_all(run)
    assert (row["status"], row["reason"]) == ("blocked", "smtp_host_not_allowed")
    assert run.mailer.sent == []


def test_checks_run_in_order(run: Deps, tmp_path: Any) -> None:
    """Unapproved, contacted and a bad host at once: check 1 is the reason recorded."""
    run.settings = make_settings(tmp_path, mail_smtp_host="smtp.gmail.com")
    seed_draft(run)
    run.memory_db.mark_contacted(LEAD, "R0", "old")
    [row] = send_all(run)
    assert row["reason"] == "not_approved"


def test_recipient_is_always_the_test_address(run: Deps) -> None:
    seed_draft(run)
    approve(run)
    [row] = send_all(run)
    assert row["status"] == SendStatus.sent and row["message_id"] == "<draft-d1@outreach-bot.local>"
    [mail] = run.mailer.sent
    assert mail.recipient == run.settings.mail_test_recipient
    assert mail.headers["To"] == run.settings.mail_test_recipient
    assert mail.headers["From"] == run.settings.mail_from
    assert mail.headers["X-Intended-Recipient"] == f"owner@{LEAD}"
    assert run.memory_db.is_blocked_or_contacted(LEAD)  # marked contacted before SMTP


def test_smtp_error_is_failed_and_not_retried(run: Deps) -> None:
    seed_draft(run)
    approve(run)
    run.mailer.fail_with = OSError("connection reset")
    [row] = send_all(run)
    assert (row["status"], row["reason"]) == ("failed", "OSError: connection reset")
    run.mailer.fail_with = None
    assert send_all(run) == [row]
    assert run.mailer.sent == []


def test_sending_becomes_unknown_on_resume_and_is_never_retried(run: Deps) -> None:
    seed_draft(run)
    approve(run)
    run.runs_db.send_begin("d1", "R1", LEAD)  # the process died during SMTP
    assert run.runs_db.mark_sending_unknown("R1") == 1
    [row] = send_all(run)
    assert row["status"] == SendStatus.unknown
    assert run.mailer.sent == []
