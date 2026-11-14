"""The qualifier subgraph and its `run_qualify` wrapper (design item 39, Subgraph contracts: Qualifier).

One lead per step: score_lead -> find_email -> decide. `decide` always writes a `qualifications` row, so
a lead never stays unqualified, and that row is what marks it done: on a re-run after a crash, leads with a
row are skipped and the others re-run from the caches (llm_cache for verdicts, hunter_cache for Hunter).
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command

from outreach.graph import Node, emit, invoke_subgraph
from outreach.models import COUNTED_STATUSES, DomainRelation, Person, ProgressEvent, RunPlan, Verdict
from outreach.qualify.email_choice import EmailCandidate, EmailChoice, choose_email
from outreach.qualify.hunter import HunterBudget, HunterOutcome, find_hunter_emails
from outreach.qualify.scoring import score_lead
from outreach.state import QualifyState
from outreach.tracing import child_config

if TYPE_CHECKING:
    from outreach.deps import Deps

ON_SITE = (DomainRelation.same_domain, DomainRelation.freemail)


def next_unqualified(deps: Deps, run_id: str) -> str | None:
    row = deps.runs_db.conn.execute(
        "SELECT c.lead_id FROM run_candidates c WHERE c.run_id = ? AND c.status IN (?, ?) AND NOT EXISTS"
        " (SELECT 1 FROM qualifications q WHERE q.run_id = c.run_id AND q.lead_id = c.lead_id)"
        " ORDER BY c.queue_pos LIMIT 1", (run_id, *COUNTED_STATUSES)).fetchone()
    return None if row is None else str(row[0])


def lead_facts(deps: Deps, run_id: str, lead_id: str) -> tuple[list[Person], str | None]:
    judge = deps.runs_db.judge_facts(run_id, lead_id)
    if not judge:
        return [], None
    return [Person.model_validate(p) for p in judge.get("people", [])], judge.get("company_name")


def candidates(deps: Deps, run_id: str, lead_id: str) -> list[EmailCandidate]:
    return [EmailCandidate.from_row(r) for r in deps.runs_db.email_observations(run_id, lead_id)]


def qualify_lead(deps: Deps, run_id: str, lead_id: str, plan: RunPlan, budget: HunterBudget,
                 config: dict | None = None) -> tuple[bool, str | None]:
    """Scores, finds an email, and writes the qualifications row. Returns (qualified, drop_reason)."""
    n = plan.checklist.threshold or len(plan.checklist.items)
    points, verdicts = score_lead(deps, run_id, lead_id, plan, config)
    people, company = lead_facts(deps, run_id, lead_id)
    profile = deps.profile

    def choose() -> EmailChoice | None:
        return choose_email(candidates(deps, run_id, lead_id), people, ownership_words=profile.ownership_words,
                            role_local_parts=profile.role_local_parts, company_name=company)

    hunter: HunterOutcome | None = None
    on_site = any(c.domain_relation in ON_SITE and not c.is_hunter for c in candidates(deps, run_id, lead_id))
    if points >= n and not on_site:
        hunter = find_hunter_emails(deps, run_id, lead_id, budget)
    chosen = choose() if points >= n else None

    if points < n:
        drop: str | None = "below_threshold"
    elif chosen is not None:
        drop = None
    elif hunter in (HunterOutcome.cap, HunterOutcome.quota):
        drop = hunter.value
    else:
        drop = "no_email"
    _decide(deps, run_id, lead_id, plan, points, verdicts, chosen if drop is None else None, drop)
    return drop is None, drop


def _decide(deps: Deps, run_id: str, lead_id: str, plan: RunPlan, points: int, verdicts: list[Verdict],
            chosen: EmailChoice | None, drop: str | None) -> None:
    deps.runs_db.write_qualification(
        run_id=run_id, lead_id=lead_id, checklist_version=plan.version, points=points,
        verdicts_json=json.dumps([v.model_dump(mode="json") for v in verdicts]), qualified=chosen is not None,
        email=chosen.email if chosen else None, email_type=chosen.email_type if chosen else None,
        email_source_kind=chosen.source_kind if chosen else None,
        email_source_url=chosen.source_url if chosen else None,
        greeting_name=chosen.greeting_name if chosen else None, drop_reason=drop)
    deps.memory_db.record_qualification(lead_id, plan.checklist.key)
    emit(ProgressEvent(kind="qualify", run_id=run_id, lead_id=lead_id,
                       status="qualified" if chosen else drop, counts={"points": points}))


def build_qualifier(deps: Deps) -> Any:
    def next_lead(state: QualifyState, config: RunnableConfig) -> Command:
        c = config.get("configurable", {})
        run_id, plan = c["run_id"], RunPlan.model_validate(c["plan"])
        lead_id = next_unqualified(deps, run_id)
        if lead_id is None:
            return Command(goto=END)
        qualify_lead(deps, run_id, lead_id, plan, c["hunter_budget"],
                     child_config(dict(config), round_=None, lead_id=lead_id, run_name=f"qualify_lead:{lead_id}"))
        return Command(update={"cursor": state.get("cursor", 0) + 1}, goto="next_lead")

    g = StateGraph(QualifyState)
    g.add_node("next_lead", next_lead, destinations=("next_lead", END))
    g.add_edge(START, "next_lead")
    return g.compile(checkpointer=False)


def make_run_qualify(deps: Deps) -> Node:
    qualifier = build_qualifier(deps)

    def run_qualify(state: dict, config: RunnableConfig) -> dict:
        run_id = state["run_id"]
        plan = deps.runs_db.plan_model(run_id)
        assert plan is not None and plan.checklist.threshold is not None
        pending = deps.runs_db.conn.execute(
            "SELECT COUNT(*) FROM run_candidates c WHERE c.run_id = ? AND c.status IN (?, ?) AND NOT EXISTS"
            " (SELECT 1 FROM qualifications q WHERE q.run_id = c.run_id AND q.lead_id = c.lead_id)",
            (run_id, *COUNTED_STATUSES)).fetchone()[0]
        child = child_config(dict(config), round_=None, run_name="qualify")
        child["configurable"].update(run_id=run_id, plan=plan.model_dump(mode="json"), hunter_budget=HunterBudget())
        child["metadata"]["run_id"] = run_id
        child["recursion_limit"] = 2 * int(pending) + 10
        invoke_subgraph(qualifier, {"cursor": 0}, child)
        return {}

    return run_qualify
