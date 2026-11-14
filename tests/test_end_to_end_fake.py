"""Phase 7: whole runs through the real `outreach run` command, with every stage real and every vendor fake.

The CLI drives the real graph: intake, the researcher over the fixture sites, the qualifier, the drafter,
review and the outbox. Only the vendors are scripted (Haiku, Opus, Exa, Hunter, SMTP), so this is the
closest a test gets to the manual check. Router: code (test_router_calls covers the Claude router).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from outreach import cli as cli_mod
from outreach.models import PausePayload, ReviewAnswer
from outreach.ui.screens import Screens
from tests.fakes import FakeChat, FakeHunter, RecordingMailer, Step
from tests.helpers import drafter_chat, parse_chat
from tests.qualify_support import ChecklistJudge
from tests.sites import ResearchEnv, exa_fixture, research_env, tool_call
from tests.test_cli import ScriptedScreens

PROMPT = "find me leads in Tucson that don't use AI"
QUERIES = ["roofing contractor Tucson AZ", "family owned roofer Tucson", "residential roof repair Tucson"]
ROUND_1_COUNTED = ["acme-roofing.test", "ortega-roofing.test", "cf-roofing.test", "mailto-only.test"]


def exa_page(*urls: str) -> dict:
    return {"results": [{"url": u, "title": u, "highlights": ["Tucson roofer"]} for u in urls],
            "cost_dollars": {"total": 0.005}}


# Round 1 is the mixed fixture; rounds 2 and 3 each find one new company, so all 3 rounds get used.
EXA_ROUNDS = [exa_fixture("tucson_mixed"), exa_page("https://soft404-roofing.test/"),
              exa_page("https://injection-roofing.test/")]


def discovery() -> FakeChat:
    """One search per round, each with different wording, then 'done'."""
    steps: list[Step] = []
    for i, q in enumerate(QUERIES, 1):
        steps += [Step("tool_calls", [tool_call(q, n=i)]), Step("ok", "Enough for this round.")]
    return FakeChat(steps, model="claude-haiku-5-5", call_site="discovery")


class E2EScreens(ScriptedScreens):
    """Approves every draft unless `review` names another action for that lead. Progress and the end screen
    are also rendered by the real Screens, so their formatting runs too."""

    def __init__(self) -> None:
        super().__init__()
        self.review: dict[str, str] = {}
        self.events: list[dict] = []
        self.real = Screens(console=self.console)

    def progress(self, chunk: Any) -> None:
        self.events.append(chunk)
        self.real.progress(chunk)

    def end(self, data: dict) -> None:
        super().end(data)
        self.real.end(data)

    def ask(self, payload: PausePayload) -> Any:
        answer = super().ask(payload)
        if payload.kind == "draft_review":
            action = self.review.get(payload.context["lead_id"], "approve")
            return ReviewAnswer(pause_id=payload.pause_id, draft_id=payload.context["draft_id"],
                                shown_hash=payload.context["shown_hash"], action=action)  # type: ignore[arg-type]
        return answer

    def kinds(self, kind: str) -> list[PausePayload]:
        return [p for p in self.asked if p.kind == kind]

    def event_leads(self, kind: str) -> list[str]:
        return [e["lead_id"] for e in self.events if e.get("kind") == kind]


@dataclass
class E2E:
    env: ResearchEnv
    screens: E2EScreens
    mailer: RecordingMailer
    judge: ChecklistJudge

    @property
    def deps(self) -> Any:
        return self.env.deps

    def run(self, *args: str) -> int:
        result = CliRunner().invoke(cli_mod.app, list(args))
        if result.exception and not isinstance(result.exception, SystemExit):
            raise result.exception
        return result.exit_code

    def run_ids(self) -> list[str]:
        return [r[0] for r in self.deps.runs_db.conn.execute("SELECT run_id FROM runs ORDER BY created_at, run_id")]

    def paid(self) -> dict[str, int]:
        return {"exa": len(self.env.exa.calls), **{k: c.call_count for k, c in self.deps.llms.items()}}


@pytest.fixture
def e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[E2E]:
    from outreach.graph.build import build_graph

    env = research_env(tmp_path, exa=list(EXA_ROUNDS), run=False)
    deps = env.deps
    env.exa.runs_db = deps.runs_db
    judge = ChecklistJudge()
    deps.llms.update(parse=parse_chat(), drafter=drafter_chat(), checklist=judge.chat, discovery=discovery())
    deps.hunter = FakeHunter(runs_db=deps.runs_db)
    mailer = RecordingMailer()
    deps.mailer = mailer
    screens = E2EScreens()
    monkeypatch.setattr(cli_mod, "make_runtime",
                        lambda: cli_mod.Runtime(deps=deps, build=build_graph, screens=screens))
    try:
        yield E2E(env, screens, mailer, judge)
    finally:
        env.close()


# --- CUJ 1 and CUJ 5 -------------------------------------------------------------------------


def test_first_run_from_a_prompt_with_one_reject(e2e: E2E) -> None:
    e2e.screens.review = {"cf-roofing.test": "reject"}
    assert e2e.run("run", PROMPT, "--router", "code") == 0
    [run_id] = e2e.run_ids()
    db, screens = e2e.deps.runs_db, e2e.screens

    # 1. Intake asks for the sender profile, because none is saved.
    assert [p.context["key"] for p in screens.kinds("question")] == ["sender_name", "business_name",
                                                                       "call_to_action"]
    # 2. The plan: Tucson, the code-checked "no AI" item, the target of 4; N is entered, then confirmed.
    first, confirmed = screens.kinds("plan")
    plan = first.context["plan"]
    assert plan["city"] == "Tucson, AZ" and plan["target_qualified"] == 4
    assert any(i["check"] == "code" and i["code_check"] == "no_ai_visible" for i in plan["checklist"]["items"])
    assert not first.context["threshold_valid"] and confirmed.context["threshold_valid"]
    # 3. A progress line for each company, rendered by the real screen.
    assert screens.event_leads("research") == ROUND_1_COUNTED
    assert "round 1 acme-roofing.test candidate" in screens.output
    # 4. The qualifier reports each lead.
    assert screens.event_leads("qualify") == ROUND_1_COUNTED
    # 5. One draft at a time, with the company's name (not its domain) on the review screen.
    reviews = screens.kinds("draft_review")
    assert [p.context["lead_id"] for p in reviews] == ROUND_1_COUNTED
    assert [p.context["position"] for p in reviews] == [1, 2, 3, 4]
    assert reviews[0].context["company_name"] == "Acme Roofing"
    assert reviews[0].context["subject"] == "Quick idea for Acme Roofing"
    # 6. Approved drafts go to the sandbox recipient only; the real address rides in a header.
    sent = e2e.mailer.sent
    assert len(sent) == 3 and {m.recipient for m in sent} == {e2e.deps.settings.mail_test_recipient}
    approved = [db.qualification(run_id, d)["email"] for d in ROUND_1_COUNTED if d != "cf-roofing.test"]
    assert [m.headers["X-Intended-Recipient"] for m in sent] == approved
    # 7. The end screen: stop reason, funnel, and numbers that match runs.db.
    [end] = screens.ended
    assert end["stop_reason"] == "complete" and end["summary"].startswith("4 of 4 qualified")
    assert end["funnel"]["qualified"] == 4 and end["funnel"]["approved"] == end["funnel"]["sent"] == 3
    assert end["usage"]["searches"] == len(e2e.env.exa.calls) == 1  # round 1 reached the target
    assert end["usage"]["pages"] == db.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    assert "4 of 4 qualified" in screens.output
    assert db.get_run(run_id)["status"] == "complete"
    # CUJ 5: the rejected company is on the do-not-contact list once review has ended.
    assert [r["domain_key"] for r in e2e.deps.memory_db.list_leads("blocked")] == ["cf-roofing.test"]


def test_a_rejected_company_is_never_drafted_again(e2e: E2E) -> None:
    """CUJ 5 step 3, across runs: the same search finds the same companies; none is drafted twice."""
    e2e.screens.review = {"cf-roofing.test": "reject"}
    assert e2e.run("run", PROMPT, "--router", "code") == 0
    e2e.env.exa.responses.clear()  # run 1 reached its target in round 1 and left rounds 2-3 unused
    e2e.env.exa.responses.extend([exa_fixture("tucson_mixed")] * 3)
    e2e.deps.llms["discovery"] = discovery()
    before = len(e2e.mailer.sent)
    assert e2e.run("run", PROMPT, "--router", "code") == 0
    second = e2e.run_ids()[-1]
    reviewed = [p.context["lead_id"] for p in e2e.screens.kinds("draft_review")]
    assert reviewed.count("cf-roofing.test") == 1 and len(e2e.mailer.sent) == before
    counted = e2e.deps.runs_db.conn.execute(
        "SELECT COUNT(*) FROM run_candidates WHERE run_id = ? AND counted = 1", (second,)).fetchone()[0]
    assert counted == 0  # blocked and contacted companies are not researched again
    assert e2e.screens.ended[-1]["summary"].startswith("0 of 4 qualified")


# --- CUJ 4 -------------------------------------------------------------------------------------


def test_not_enough_leads(e2e: E2E) -> None:
    """After round 1 only 2 qualify; rounds 2 and 3 search again with new wording; the outbox runs anyway."""
    for domain in ("ortega-roofing.test", "cf-roofing.test"):
        e2e.judge.by_domain[domain] = {"i2": ("not_met", None), "i3": ("not_met", None)}  # 1 point < N=2
    assert e2e.run("run", PROMPT, "--router", "code") == 0
    [run_id] = e2e.run_ids()
    # 1. Three rounds, three different wordings, the same city.
    assert [q for q, _n in e2e.env.exa.calls] == QUERIES
    assert e2e.deps.runs_db.plan_model(run_id).city == "Tucson, AZ"
    assert [e["round"] for e in e2e.screens.events if e.get("kind") == "round_closed"] == [1, 2, 3]
    # 2. The code router never asks (only intake's questions); the outbox runs for the 2 it has.
    assert len(e2e.screens.kinds("question")) == 3
    assert [p.context["lead_id"] for p in e2e.screens.kinds("draft_review")] == ["acme-roofing.test",
                                                                                   "mailto-only.test"]
    assert len(e2e.mailer.sent) == 2
    # 3. The end screen.
    [end] = e2e.screens.ended
    assert end["summary"] == "2 of 4 qualified: research rounds used up"
    assert end["drop_reasons"]["below_threshold"] == 2
    assert end["usage"]["searches"] == 3


# --- CUJ 3 -------------------------------------------------------------------------------------


def test_crash_during_research_then_resume(e2e: E2E) -> None:
    """The judge dies on company 3 (cf-roofing). The run fails with a saved trace; `outreach run` offers to
    resume; companies 1-2 keep their results and nothing they already paid for is paid again."""
    judge = e2e.deps.llms["judge"]
    reply = judge.responder
    crashed: list[bool] = []

    def dying(messages: list[Any]) -> Any:
        if "cf-roofing.test" in messages[-1].content and not crashed:
            crashed.append(True)
            raise RuntimeError("process died")
        return reply(messages)

    judge.responder = dying
    assert e2e.run("run", PROMPT, "--router", "code") == 1
    [run_id] = e2e.run_ids()
    db = e2e.deps.runs_db
    assert db.get_run(run_id)["status"] == "failed" and "Run failed: RuntimeError" in e2e.screens.output
    kept = {r["lead_id"]: r["status"] for r in db.conn.execute(
        "SELECT lead_id, status FROM run_candidates WHERE run_id = ?", (run_id,))}
    assert kept["acme-roofing.test"] == kept["ortega-roofing.test"] == "candidate"  # 1. kept
    paid = e2e.paid()
    fetched_before = Counter(e2e.env.server.requests)

    e2e.screens.inputs.append("resume")
    assert e2e.run("run") == 0  # the unfinished run is offered; resume
    assert f"banner {run_id}" in e2e.screens.output
    after = e2e.paid()
    assert after["exa"] == paid["exa"] and after["triage"] == paid["triage"]  # no search or triage repeated
    assert after["judge"] == paid["judge"] + 2  # 2. company 3 again (the call that died), then company 4
    refetched = Counter(e2e.env.server.requests) - fetched_before
    assert not any("acme-roofing" in u or "ortega-roofing" in u for u in refetched)
    assert db.get_run(run_id)["status"] == "complete"  # 3. the batch finished normally
    assert e2e.screens.ended[-1]["summary"].startswith("4 of 4 qualified")
