"""Pause nodes: `ask_user` and `review_next`, plus the shared interrupt helper.

Rule (design: Pause contract): nothing happens before `interrupt()` except deterministic database
reads. A resumed node re-runs from its first line, so the payload and its `pause_id` come out the same.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.messages import HumanMessage
from langgraph.types import Command, interrupt
from pydantic import TypeAdapter, ValidationError

from outreach.db.runs_db import text_hash
from outreach.graph import Node
from outreach.graph.plan_edits import answer_error, apply_answer
from outreach.llm import LLMContext, load_prompt, structured_call
from outreach.models import (
    SUPERVISOR_KEYS,
    Answer,
    IntakeReply,
    PausePayload,
    PendingQuestion,
    QuestionAnswer,
    QuestionKey,
    ReviewAction,
    ReviewAnswer,
)

if TYPE_CHECKING:
    import sqlite3

    from outreach.deps import Deps
    from outreach.state import ParentState

ANSWER: TypeAdapter[Any] = TypeAdapter(Answer)
MISMATCH = "__mismatch__"
INTAKE_PURPOSE = {
    "city": "the US city to search for roofing companies in, for example 'Tucson, AZ'.",
    "sender_name": "the name every email is signed with: the user's own name as readers should see it, "
                   "for example 'Sam Lee'.",
    "business_name": "the user's business, shown under their name in each email's signature, "
                     "for example 'Lee Automation'.",
    "call_to_action": "the one thing each email asks the reader to do, for example "
                      "'reply for a 15-minute call'.",
}


@dataclass(frozen=True)
class Explain:
    """A `check` result: not an answer. Show the same question again with this help text."""

    text: str
REJECT_NOTE = "blocks this company forever; use edit for a bad draft"


def ask(payload: PausePayload, check: Callable[[Any], str | Explain | None]) -> Any:
    """Interrupts until the answer passes `check`.

    `check` returns None (accept), MISMATCH (stale pause_id or hash: show the same payload again), an
    error message (shown in `context["error"]`), or Explain (shown in `context["explanation"]`). Each retry
    is another `interrupt()` in the same node; LangGraph matches resume values to interrupts by order.
    """
    shown = payload
    while True:
        raw = interrupt(shown.model_dump(mode="json"))
        try:
            answer = ANSWER.validate_python(raw)
        except ValidationError:
            result: str | Explain | None = MISMATCH
        else:
            result = MISMATCH if answer.kind != payload.kind or answer.pause_id != payload.pause_id \
                else check(answer)
        if result is None:
            return answer
        if isinstance(result, Explain):
            shown = payload.model_copy(update={"context": {**payload.context, "explanation": result.text}})
        else:
            shown = payload if result == MISMATCH else \
                payload.model_copy(update={"context": {**payload.context, "error": result}})


def explain_if_question(deps: Deps, run_id: str, key: str, question: str, reply: str) -> Explain | None:
    """Haiku reads an intake reply. A question back ("what do you mean") gets an answer and the same
    question again, instead of becoming the value. Cached by llm_cache, so replaying the node on resume
    is free and gives the same result. Any failure counts as an answer: a model problem never blocks intake."""
    llm = deps.llms.get("intake_reply")
    if llm is None:
        return None
    template, version = load_prompt("intake_reply")
    prompt = template.format(purpose=INTAKE_PURPOSE.get(key, key), question=question, reply=reply)
    ctx = LLMContext(runs_db=deps.runs_db, settings=deps.settings, run_id=run_id, prompt_version=version,
                     ids=deps.ids)
    out = structured_call(llm, IntakeReply, [HumanMessage(content=prompt)], "intake_reply", ctx=ctx)
    text = (out.reply or "").strip() if out is not None and out.kind == "question" else ""
    return Explain(text) if text else None


def make_ask_user(deps: Deps) -> Node:
    def ask_user(state: ParentState) -> Command:
        run_id = state["run_id"]
        if not state.get("pending_question"):
            return Command(goto="supervisor")
        pq = PendingQuestion.model_validate(state["pending_question"])
        plan = deps.runs_db.plan_model(run_id)
        context: dict[str, Any] = {"key": pq.key.value, "origin": pq.origin,
                                   "can_remember": pq.key.value in SUPERVISOR_KEYS}
        if pq.key == QuestionKey.city:
            context["suggestions"] = list(deps.profile.fallback_cities)
        payload = PausePayload(kind="question", pause_id=f"q:{run_id}:{pq.key.value}", prompt_text=pq.prompt_text,
                               allowed_answers=pq.allowed_answers, context=context)

        def check(a: Any) -> str | Explain | None:
            if not isinstance(a, QuestionAnswer) or a.key != pq.key:
                return MISMATCH
            free_text = pq.origin == "intake" and not pq.allowed_answers
            if free_text and (help_text := explain_if_question(deps, run_id, pq.key.value, pq.prompt_text,
                                                               a.value)):
                return help_text
            return answer_error(pq.key, a.value, plan)

        answer: QuestionAnswer = ask(payload, check)
        value = answer.value.strip()
        deps.runs_db.record_answer(pause_id=payload.pause_id, run_id=run_id, key=pq.key.value, value=value,
                                   origin=pq.origin)
        apply_answer(deps, run_id, pq.key, value)
        if answer.remember and pq.key.value in SUPERVISOR_KEYS:
            deps.memory_db.remember_preference(pq.key.value, value)
        goto = "intake_missing" if pq.origin == "intake" else "supervisor"
        return Command(goto=goto, update={"pending_question": None})

    return ask_user


def review_keys(decided: int) -> dict[str, str]:
    """`back` reopens the last decision, so it is offered only once there is one."""
    keys = {"a": "approve", "e": "edit (saving approves)", "r": f"reject: {REJECT_NOTE}", "s": "skip"}
    if decided:
        keys["b"] = "back"
    return keys


def review_payload(deps: Deps, run_id: str, item: sqlite3.Row) -> PausePayload:
    db = deps.runs_db
    lead_id = item["lead_id"]
    q = db.qualification(run_id, lead_id)
    plan = db.plan_model(run_id)
    judge = db.judge_facts(run_id, lead_id)
    items = {i.id: i.text for i in plan.checklist.items} if plan else {}
    verdicts = json.loads(q["verdicts_json"]) if q is not None else []
    for v in verdicts:
        v.setdefault("text", items.get(v.get("item_id", ""), ""))
    decided, total = db.review_progress(run_id)
    context = {
        "draft_id": item["draft_id"], "shown_hash": item["text_hash"], "lead_id": lead_id,
        "company_name": judge.get("company_name") or lead_id,
        "subject": item["subject"], "body": item["body"],
        "position": decided + 1, "total": total,
        "verdicts": verdicts, "points": q["points"] if q is not None else None,
        "threshold": plan.checklist.threshold if plan else None,
        "email": q["email"] if q is not None else None,
        "email_type": q["email_type"] if q is not None else None,
        "email_source_kind": q["email_source_kind"] if q is not None else None,
        "email_source_url": q["email_source_url"] if q is not None else None,
        "real_recipient": q["email"] if q is not None else None,
        "test_recipient": deps.settings.mail_test_recipient,
        "claim_flags": json.loads(item["claim_flags_json"] or "[]"),
        "keys": review_keys(decided),
    }
    return PausePayload(kind="draft_review",
                        pause_id=f"review:{item['draft_id']}:{db.next_review_seq(item['draft_id'])}",
                        prompt_text=f"Draft {decided + 1} of {total}: {context['company_name']}",
                        allowed_answers=[a.value for a in ReviewAction], context=context)


def make_review_next(deps: Deps) -> Node:
    def review_next(state: ParentState) -> Command:
        run_id = state["run_id"]
        db = deps.runs_db
        item = db.next_review_item(run_id)
        if item is None:
            # Review is over: only now do final rejects become permanent blocks.
            deps.memory_db.block_many(db.final_rejects(run_id), run_id, reason="rejected")
            return Command(goto="send_next")
        payload = review_payload(deps, run_id, item)

        def check(a: Any) -> str | None:
            if not isinstance(a, ReviewAnswer) or a.draft_id != item["draft_id"] \
                    or a.shown_hash != item["text_hash"]:
                return MISMATCH
            if a.action == ReviewAction.edit and not ((a.subject or "").strip() and (a.body or "").strip()):
                return "an edit needs a subject and a body"
            if a.action == ReviewAction.back and db.review_progress(run_id)[0] == 0:
                return "there is no earlier draft to go back to"
            return None

        answer: ReviewAnswer = ask(payload, check)
        draft_id = item["draft_id"]
        if answer.action == ReviewAction.approve:
            db.record_review(draft_id, "approve", final_subject=item["subject"], final_body=item["body"],
                             approved_hash=text_hash(item["subject"], item["body"]))
        elif answer.action == ReviewAction.edit:
            subject, body = (answer.subject or "").strip(), (answer.body or "").strip()
            db.record_review(draft_id, "edit", final_subject=subject, final_body=body,
                             approved_hash=text_hash(subject, body))
        elif answer.action == ReviewAction.reject:
            db.record_review(draft_id, "reject")
        elif answer.action == ReviewAction.skip:
            db.skip(draft_id)
        else:
            db.back(run_id, draft_id)
        return Command(goto="review_next")

    return review_next
