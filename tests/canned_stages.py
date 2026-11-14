"""Canned `run_research` and `run_qualify` nodes for phase 3 (design item 52).

They write fixture rows to run_candidates, research_rounds, evidence and qualifications, the same
tables the real wrappers write in phases 4 and 5, and return nothing else.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from outreach.deps import Deps
from outreach.graph import emit
from outreach.models import COUNTED_STATUSES, ProgressEvent, ResearchStatus, StageStopReason


@dataclass
class CannedLead:
    lead_id: str
    status: ResearchStatus = ResearchStatus.candidate
    points: int = 3
    email: str | None = "default"  # "default" -> owner@<lead_id>; None -> no email found
    company_name: str | None = None
    greeting_name: str | None = "Pat"

    @property
    def address(self) -> str | None:
        return f"owner@{self.lead_id}" if self.email == "default" else self.email


def lead(lead_id: str, **kw: Any) -> CannedLead:
    return CannedLead(lead_id, **kw)


@dataclass
class CannedStages:
    deps: Deps
    rounds: list[list[CannedLead]]
    calls: Counter = field(default_factory=Counter)

    def _spec(self, lead_id: str) -> CannedLead:
        return next(c for r in self.rounds for c in r if c.lead_id == lead_id)

    def run_research(self, state: dict) -> dict:
        run_id = state["run_id"]
        db = self.deps.runs_db
        plan = db.plan_model(run_id)
        assert plan is not None and plan.status == "frozen"
        round_ = db.open_round(run_id, plan.version)
        self.calls["research"] += 1
        leads = self.rounds[round_ - 1] if round_ <= len(self.rounds) else []
        db.queue_candidates(run_id, round_, [c.lead_id for c in leads], found_by_query="canned")
        counted = 0
        for c in leads:
            row = db.candidate(run_id, c.lead_id)
            if row is not None and row["round"] == round_ and row["status"] in COUNTED_STATUSES:
                counted += 1  # finished before a crash; progress is rebuilt from runs.db
                continue
            if row is None or row["round"] != round_ or row["status"] not in (
                    ResearchStatus.queued, ResearchStatus.researching):
                continue
            if not self.deps.memory_db.claim(c.lead_id, run_id, plan.checklist.key):
                continue
            if c.status != ResearchStatus.rejected_triage:
                db.set_candidate_status(run_id, c.lead_id, ResearchStatus.researching)
            name = c.company_name or c.lead_id.split(".")[0].replace("-", " ").title()
            db.add_evidence(evidence_id=f"ev-{run_id}-{c.lead_id}", run_id=run_id, lead_id=c.lead_id, kind="name",
                            quote=f"{name} has served {plan.city} since 1998")
            db.set_candidate_status(run_id, c.lead_id, c.status, judge_json=json.dumps({"judge": {"company_name": name}}),  # noqa: E501
                                    drop_reason=None if c.status in COUNTED_STATUSES else c.status.value)
            self.deps.memory_db.finalize(c.lead_id, run_id, c.status)
            counted += c.status in COUNTED_STATUSES
            emit(ProgressEvent(kind="research", run_id=run_id, round=round_, lead_id=c.lead_id, status=c.status))
        db.close_round(run_id, round_, new_companies=counted,
                       stop_reason=StageStopReason.queue_empty if leads else StageStopReason.limit_searches)
        return {}

    def run_qualify(self, state: dict) -> dict:
        run_id = state["run_id"]
        db = self.deps.runs_db
        plan = db.plan_model(run_id)
        assert plan is not None and plan.checklist.threshold is not None
        n = plan.checklist.threshold
        self.calls["qualify"] += 1
        rows = db.conn.execute(
            "SELECT c.lead_id FROM run_candidates c WHERE c.run_id = ? AND c.status IN (?, ?) AND NOT EXISTS"
            " (SELECT 1 FROM qualifications q WHERE q.run_id = c.run_id AND q.lead_id = c.lead_id)"
            " ORDER BY c.queue_pos", (run_id, *COUNTED_STATUSES)).fetchall()
        for (lead_id,) in rows:
            spec = self._spec(lead_id)
            verdicts = [{"item_id": item.id, "value": "met" if i < spec.points else "not_met",
                         "quote": None, "page_id": None} for i, item in enumerate(plan.checklist.items)]
            email = spec.address
            qualified = spec.points >= n and email is not None
            drop = None if qualified else ("below_threshold" if spec.points < n else "no_email")
            db.write_qualification(
                run_id=run_id, lead_id=lead_id, checklist_version=plan.version, points=spec.points,
                verdicts_json=json.dumps(verdicts), qualified=qualified, email=email,
                email_type="owner" if email else None, email_source_kind="page" if email else None,
                email_source_url=f"https://{lead_id}/contact" if email else None,
                greeting_name=spec.greeting_name if email else None, drop_reason=drop)
            self.deps.memory_db.record_qualification(lead_id, plan.checklist.key)
            emit(ProgressEvent(kind="qualify", run_id=run_id, lead_id=lead_id,
                               status="qualified" if qualified else drop))
        return {}

    def nodes(self) -> dict[str, Any]:
        return {"run_research": self.run_research, "run_qualify": self.run_qualify}
