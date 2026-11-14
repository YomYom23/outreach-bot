"""Intake: a fixed chain with no supervisor. parse_request -> fill_from_memory -> intake_missing <-> ask_user
-> confirm_plan (pause, self-loop) -> supervisor."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from outreach.graph import Node
from outreach.graph.pauses import MISMATCH, ask
from outreach.graph.plan_edits import EDIT_KEYS, apply_edits, effective_signature, plan_checklist_key
from outreach.llm import LLMContext, load_prompt, structured_call
from outreach.models import (
    INTAKE_KEYS,
    Checklist,
    ChecklistItem,
    PausePayload,
    PendingQuestion,
    PlanAnswer,
    QuestionKey,
    RunPlan,
    SenderProfile,
)

if TYPE_CHECKING:
    from outreach.deps import Deps
    from outreach.state import ParentState


class ParsedItem(BaseModel):
    text: str
    code_check: str | None = None


class ParsedRequest(BaseModel):
    city: str | None = None
    target: int | None = None
    items: list[ParsedItem] = Field(default_factory=list)


def make_parse_request(deps: Deps) -> Node:
    def parse_request(state: ParentState) -> dict:
        run_id = state["run_id"]
        run = deps.runs_db.get_run(run_id)
        assert run is not None, f"the CLI inserts the runs row before streaming ({run_id})"
        if run["parsed_json"] is not None:  # resumed after this node finished its write
            return {}
        template, version = load_prompt("parse_request")
        phrases = "\n".join(f"- {k}: {', '.join(v)}" for k, v in deps.profile.code_check_phrases.items())
        text = template.format(industry=deps.profile.industry, code_checks=", ".join(deps.profile.code_checks),
                               code_check_phrases=phrases, prompt=run["prompt"])
        ctx = LLMContext(runs_db=deps.runs_db, settings=deps.settings, run_id=run_id, prompt_version=version,
                         ids=deps.ids)
        out = structured_call(deps.llms["parse"], ParsedRequest, [HumanMessage(content=text)], "parse", ctx=ctx)
        parsed = out or ParsedRequest()  # nothing back: intake asks for every field
        for item in parsed.items:
            if item.code_check not in deps.profile.code_checks:
                item.code_check = None
        if run["city_override"]:
            parsed.city = run["city_override"]
        deps.runs_db.set_parsed(run_id, parsed.model_dump(mode="json"))
        return {}

    return parse_request


def _merge_items(parsed: list[dict], deps: Deps) -> list[ChecklistItem]:
    """Items from the prompt first, then TargetProfile defaults that aren't already covered."""
    out: list[ChecklistItem] = []
    seen_text: set[str] = set()
    seen_checks: set[str] = set()
    candidates = [(p["text"], p.get("code_check")) for p in parsed] + \
        [(i.text, i.code_check) for i in deps.profile.default_checklist.items]
    for text, code_check in candidates:
        norm = " ".join(text.split()).casefold()
        if norm in seen_text or (code_check and code_check in seen_checks):
            continue
        seen_text.add(norm)
        if code_check:
            seen_checks.add(code_check)
        out.append(ChecklistItem(id=f"i{len(out) + 1}", text=text, check="code" if code_check else "llm",
                                 code_check=code_check))
    return out


def make_fill_from_memory(deps: Deps) -> Node:
    def fill_from_memory(state: ParentState) -> dict:
        run_id = state["run_id"]
        db = deps.runs_db
        if db.latest_plan(run_id) is not None:
            return {}
        run = db.get_run(run_id)
        assert run is not None
        parsed = json.loads(run["parsed_json"] or "{}")
        remembered = deps.memory_db.latest_checklist()
        if remembered is not None:
            items = [ChecklistItem.model_validate({**i, "id": i.get("id") or f"i{n}"})
                     for n, i in enumerate(json.loads(remembered["items_json"]), 1)]
            threshold = remembered["threshold"]
        else:
            # N has no default: confirm_plan collects it (design: Decisions, Threshold N).
            items, threshold = _merge_items(parsed.get("items", []), deps), None
        defaults = deps.profile.sender
        sender = SenderProfile(offer_text=defaults.offer_text, tone=defaults.tone, max_words=defaults.max_words)
        rs = deps.memory_db.latest_sender()
        if rs is not None:
            sender.name, sender.business_name, sender.call_to_action = \
                rs["name"], rs["business_name"], rs["call_to_action"]
        plan = RunPlan(run_id=run_id, version=1, status="draft", city=parsed.get("city"),
                       target_qualified=parsed.get("target") or deps.settings.target_qualified,
                       checklist=Checklist(items=items, threshold=threshold), sender=sender)
        db.write_plan_model(plan)
        return {}

    return fill_from_memory


def make_intake_missing(deps: Deps) -> Node:
    def intake_missing(state: ParentState) -> Command:
        plan = deps.runs_db.plan_model(state["run_id"])
        assert plan is not None
        values = {"city": plan.city, "sender_name": plan.sender.name, "business_name": plan.sender.business_name,
                  "call_to_action": plan.sender.call_to_action}
        for key in INTAKE_KEYS:
            if not (values[key] or "").strip():
                pq = PendingQuestion(key=QuestionKey(key), origin="intake",
                                     prompt_text=deps.profile.question_templates[key])
                return Command(goto="ask_user", update={"pending_question": pq.model_dump(mode="json")})
        return Command(goto="confirm_plan")

    return intake_missing


def plan_context(plan: RunPlan, deps: Deps) -> dict[str, Any]:
    return {
        "plan": plan.model_dump(mode="json"),
        "signature": effective_signature(plan.sender, deps.profile),
        "edit_keys": list(EDIT_KEYS),
        "threshold_valid": plan.checklist.threshold_valid(),
        "remember_choices": ["sender", "checklist"],
    }


def make_confirm_plan(deps: Deps, after_confirm: str) -> Node:
    def confirm_plan(state: ParentState) -> Command:
        run_id = state["run_id"]
        db = deps.runs_db
        plan = db.plan_model(run_id)
        assert plan is not None
        if plan.status == "frozen":  # resumed after the freeze was written
            return Command(goto=after_confirm)
        payload = PausePayload(kind="plan", pause_id=f"plan:{run_id}:{plan.version}",
                               prompt_text="Confirm the plan, or edit it.", allowed_answers=["confirm", "edit"],
                               context=plan_context(plan, deps))

        def check(a: Any) -> str | None:
            if not isinstance(a, PlanAnswer):
                return MISMATCH
            if a.action == "confirm":
                if not plan.checklist.threshold_valid():
                    return f"set the threshold N (1 to {len(plan.checklist.items)}) before confirming"
                return None
            try:
                apply_edits(plan, a.edits, deps.profile)
            except ValueError as e:
                return str(e)
            return None

        answer: PlanAnswer = ask(payload, check)
        if answer.action == "edit":
            edited = apply_edits(plan, answer.edits, deps.profile)
            edited.version = db.new_plan_version(run_id)
            edited.checklist.version = edited.version
            db.write_plan_model(edited)
            return Command(goto="confirm_plan")
        frozen = plan.model_copy(deep=True)
        frozen.status = "frozen"
        frozen.checklist.key = plan_checklist_key(frozen)
        db.write_plan_model(frozen)
        if "sender" in answer.remember:
            deps.memory_db.remember_sender(frozen.sender.name, frozen.sender.business_name,
                                           frozen.sender.call_to_action)
        if "checklist" in answer.remember:
            deps.memory_db.remember_checklist([i.model_dump() for i in frozen.checklist.items],
                                              frozen.checklist.threshold)
        return Command(goto=after_confirm)

    return confirm_plan
