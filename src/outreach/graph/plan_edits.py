"""Plan changes shared by intake, `confirm_plan`, `ask_user` and remembered supervisor answers."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from outreach.db.memory_db import checklist_key
from outreach.models import ChecklistItem, QuestionKey, RunPlan, SenderProfile

if TYPE_CHECKING:
    from outreach.config import TargetProfile
    from outreach.deps import Deps

YES = frozenset({"yes", "y"})
NO = frozenset({"no", "n"})

SENDER_FIELDS = tuple(SenderProfile.model_fields)
EDIT_KEYS = ("city", "target", "threshold", *(f"sender.{f}" for f in SENDER_FIELDS),
             "item.add", "item.<id>.text", "item.<id>.remove")
_ITEM_KEY = re.compile(r"^item\.([A-Za-z0-9_]+)\.(text|remove)$")


def classify_item(text: str, profile: TargetProfile) -> tuple[str, str | None]:
    """(check, code_check): a code check when the text contains one of its phrases."""
    low = text.casefold()
    for code_check, phrases in profile.code_check_phrases.items():
        if code_check in profile.code_checks and any(p.casefold() in low for p in phrases):
            return "code", code_check
    return "llm", None


def effective_signature(sender: SenderProfile, profile: TargetProfile) -> str:
    if sender.signature:
        return sender.signature
    return profile.sender.signature_template.format(name=sender.name, business_name=sender.business_name).strip()


def items_for_key(plan: RunPlan) -> list[dict]:
    return [i.model_dump() for i in plan.checklist.items]


def plan_checklist_key(plan: RunPlan) -> str:
    return checklist_key(items_for_key(plan), plan.checklist.threshold)


def _next_item_id(plan: RunPlan) -> str:
    nums = [int(i.id[1:]) for i in plan.checklist.items if i.id[1:].isdigit()]
    return f"i{max(nums, default=0) + 1}"


def _positive_int(key: str, value: str) -> int:
    try:
        n = int(value)
    except ValueError:
        raise ValueError(f"{key} must be a whole number, not {value!r}") from None
    if n < 1:
        raise ValueError(f"{key} must be at least 1")
    return n


def apply_edits(plan: RunPlan, edits: dict[str, str], profile: TargetProfile) -> RunPlan:
    """Returns a changed copy of `plan` (same version). Raises ValueError naming the bad key."""
    p = plan.model_copy(deep=True)
    for key, raw in edits.items():
        value = raw.strip()
        if key == "city":
            if not value:
                raise ValueError("city can't be empty")
            p.city = value
        elif key == "target":
            p.target_qualified = _positive_int(key, value)
        elif key == "threshold":
            p.checklist.threshold = _positive_int(key, value) if value else None
        elif key.startswith("sender.") and key[7:] in SENDER_FIELDS:
            field = key[7:]
            setattr(p.sender, field, _positive_int(key, value) if field == "max_words" else raw.strip("\n"))
        elif key == "item.add":
            if not value:
                raise ValueError("item.add needs the item text")
            check, code_check = classify_item(value, profile)
            p.checklist.items.append(ChecklistItem(id=_next_item_id(p), text=value, check=check,  # type: ignore[arg-type]
                                                   code_check=code_check))
        elif m := _ITEM_KEY.match(key):
            item_id, op = m.groups()
            match = [i for i in p.checklist.items if i.id == item_id]
            if not match:
                raise ValueError(f"no checklist item {item_id}")
            if op == "remove":
                p.checklist.items = [i for i in p.checklist.items if i.id != item_id]
            else:
                if not value:
                    raise ValueError(f"{key} can't be empty")
                check, code_check = classify_item(value, profile)
                match[0].text, match[0].check, match[0].code_check = value, check, code_check  # type: ignore[assignment]
        else:
            raise ValueError(f"unknown edit key {key!r}")
    p.checklist.key = ""
    return p


def answer_error(key: QuestionKey, value: str, plan: RunPlan | None) -> str | None:
    """Why an answer to `key` is invalid, or None."""
    v = value.strip().lower()
    if not v:
        return "the answer can't be empty"
    if key == QuestionKey.extra_round and v not in YES | NO:
        return "answer yes or no"
    if key == QuestionKey.lower_threshold and v not in NO:
        n = plan.checklist.threshold if plan else None
        if not v.isdigit() or n is None or not 1 <= int(v) <= n - 1:
            return f"type a number from 1 to {(n or 2) - 1}, or 'no'"
    return None


def apply_answer(deps: Deps, run_id: str, key: QuestionKey, value: str) -> None:
    """The effect of an answer (design: Questions the supervisor can ask; intake keys)."""
    db = deps.runs_db
    plan = db.plan_model(run_id)
    assert plan is not None, "answers are applied after fill_from_memory wrote plan version 1"
    v = value.strip()
    new: RunPlan | None = None
    if key == QuestionKey.city:
        new = plan.model_copy(update={"city": v})
    elif key in (QuestionKey.sender_name, QuestionKey.business_name, QuestionKey.call_to_action):
        field = {"sender_name": "name", "business_name": "business_name",
                 "call_to_action": "call_to_action"}[key.value]
        new = plan.model_copy(update={"sender": plan.sender.model_copy(update={field: v})})
    elif key == QuestionKey.extra_round:
        if v.lower() in YES:
            db.add_override(run_id, "extra_round", "1")
    elif key == QuestionKey.change_city:
        if v.lower() not in NO:
            new = plan.model_copy(update={"city": v})
            db.add_override(run_id, "extra_round", "1")
    elif key == QuestionKey.lower_threshold and v.lower() not in NO:
        checklist = plan.checklist.model_copy(update={"threshold": int(v)})
        new = plan.model_copy(update={"checklist": checklist})
        new.checklist.key = plan_checklist_key(new)
    if new is not None:
        new.version = db.new_plan_version(run_id)
        new.checklist.version = new.version
        db.write_plan_model(new)
        if key == QuestionKey.lower_threshold:
            assert new.checklist.threshold is not None
            db.rescore(run_id, new.checklist.threshold, new.version)
