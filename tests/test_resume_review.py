"""Phase 3 exit test: CUJ 2 (close the terminal mid-review and resume) and the review actions."""

from __future__ import annotations

from typing import Any

from outreach.deps import Deps
from outreach.models import PausePayload
from tests.helpers import Driver, lead, make_graph
from tests.test_pauses import reopen

FOUR = [[lead("acme-roofing.test"), lead("best-roof.test"), lead("copper-top.test"), lead("desert-roofs.test")]]


def to_review(deps: Deps, rounds: Any = None) -> tuple[Driver, PausePayload]:
    graph, _ = make_graph(deps, rounds or FOUR)
    d = Driver(graph, deps)
    d.create_run()
    p = d.through_intake(d.start())
    assert p is not None and p.kind == "draft_review"
    return d, p


def ids(p: PausePayload | None) -> str:
    assert p is not None and p.kind == "draft_review", p
    return str(p.context["lead_id"])


def finish(d: Driver, p: PausePayload | None, action: str = "approve") -> None:
    while p is not None:
        p = d.review(p, action)


def test_cuj2_resume_shows_same_draft_and_repeats_nothing(deps_fake: Deps) -> None:
    d, p = to_review(deps_fake)
    assert p.prompt_text.startswith("Draft 1 of 4")
    p2 = d.review(p, "approve")
    assert p2 is not None and p2.prompt_text.startswith("Draft 2 of 4")
    drafter_calls = len(deps_fake.llms["drafter"].calls)
    api_calls = len(deps_fake.runs_db.api_calls("R1"))

    deps2 = reopen(deps_fake)  # the terminal closed; a new process opens the same files
    graph2, stages2 = make_graph(deps2, FOUR)
    d2 = Driver(graph2, deps2)
    again = d2.resume_none()
    assert again == p2  # same pause_id, same text
    assert deps2.llms["drafter"].calls == [] and deps2.llms["parse"].calls == []
    assert stages2.calls == {}  # no research or qualify call repeated
    assert len(deps2.runs_db.api_calls("R1")) == api_calls
    assert len(deps_fake.llms["drafter"].calls) == drafter_calls
    assert deps_fake.mailer.sent == []  # nothing sent before review ends

    finish(d2, again)
    assert len(deps_fake.mailer.sent) == 4


def test_skip_moves_draft_to_end(deps_fake: Deps) -> None:
    d, p = to_review(deps_fake)
    first = ids(p)
    p = d.review(p, "skip")
    order = [ids(p)]
    for _ in range(3):
        p = d.review(p, "approve")  # type: ignore[arg-type]
        order.append(ids(p) if p else "end")
    assert order[:3] == ["best-roof.test", "copper-top.test", "desert-roofs.test"]
    assert order[3] == first  # the skipped draft comes back last
    assert p is not None and p.prompt_text.startswith("Draft 4 of 4")


def test_back_passes_over_skips(deps_fake: Deps) -> None:
    d, p = to_review(deps_fake)
    p = d.review(p, "approve")  # acme approved
    assert ids(p) == "best-roof.test"
    p = d.review(p, "skip")  # best skipped (not a decision)
    assert ids(p) == "copper-top.test"
    p = d.review(p, "back")
    assert ids(p) == "acme-roofing.test"  # reopens the last decision, passing over the skip
    assert deps_fake.runs_db.final_review(p.context["draft_id"]) is None  # type: ignore[union-attr]


def test_back_is_not_offered_on_the_first_draft(deps_fake: Deps) -> None:
    """Manual check: nothing to go back to, so no `b`; a stale `b` is refused and the draft is shown again."""
    d, p = to_review(deps_fake)
    assert "b" not in p.context["keys"]
    again = d.review(p, "back")
    assert again is not None and ids(again) == ids(p)
    assert again.context["error"] == "there is no earlier draft to go back to"
    nxt = d.review(again, "approve")
    assert nxt is not None and "b" in nxt.context["keys"]


def test_back_over_reject_leaves_no_block(deps_fake: Deps) -> None:
    d, p = to_review(deps_fake)
    p = d.review(p, "reject")
    p = d.review(p, "back")  # type: ignore[arg-type]
    assert ids(p) == "acme-roofing.test"
    finish(d, p)
    assert deps_fake.memory_db.list_leads("blocked") == []
    assert len(deps_fake.mailer.sent) == 4


def test_reject_blocks_only_when_review_ends_and_is_never_sent(deps_fake: Deps) -> None:
    d, p = to_review(deps_fake)
    p = d.review(p, "reject")
    assert deps_fake.memory_db.list_leads("blocked") == []  # back can still undo it
    finish(d, p)
    blocked = [r["domain_key"] for r in deps_fake.memory_db.list_leads("blocked")]
    assert blocked == ["acme-roofing.test"]
    sent_to = {m.headers["X-Intended-Recipient"] for m in deps_fake.mailer.sent}
    assert "owner@acme-roofing.test" not in sent_to and len(sent_to) == 3
    assert deps_fake.memory_db.should_skip("acme-roofing.test", None)  # CUJ 5: never again


def test_edit_sends_the_edited_text(deps_fake: Deps) -> None:
    d, p = to_review(deps_fake, [[lead("acme-roofing.test")]])
    p = d.review(p, "edit", subject="  New subject ", body="New body\n\nSam")
    assert p is None
    [mail] = deps_fake.mailer.sent
    assert (mail.subject, mail.body) == ("New subject", "New body\n\nSam")


def test_review_payload_shows_real_and_test_recipient(deps_fake: Deps) -> None:
    _d, p = to_review(deps_fake)
    ctx = p.context
    assert ctx["real_recipient"] == "owner@acme-roofing.test"
    assert ctx["test_recipient"] == deps_fake.settings.mail_test_recipient
    assert "blocks this company forever" in ctx["keys"]["r"]
    assert ctx["subject"] == "Quick idea for Acme Roofing" and ctx["body"].endswith("Sam Lee\nLee Automation")
    assert [v["value"] for v in ctx["verdicts"]] == ["met", "met", "met"]
