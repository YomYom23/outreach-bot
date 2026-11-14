"""The outbox: drafter -> review_next (pauses) -> send_next (guarded). It drains before the supervisor
sees the run again."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel

from outreach.db.runs_db import text_hash
from outreach.graph import Node, emit
from outreach.graph.plan_edits import effective_signature
from outreach.llm import LLMContext, load_prompt, structured_call
from outreach.models import ProgressEvent, SendStatus

if TYPE_CHECKING:
    from outreach.deps import Deps
    from outreach.state import ParentState

APPROVED_ACTIONS = ("approve", "edit")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’&.-]*")


class DraftOut(BaseModel):
    subject: str
    body: str


def check_claims(text: str, allowed_texts: Iterable[str], vocabulary: Iterable[str]) -> list[str]:
    """Flags digit runs and Title-Case words (not at a sentence start) that no allowed source contains."""
    corpus = " ".join(allowed_texts)
    allowed_words = {w.casefold().strip(".'’") for w in _TOKEN.findall(corpus)} | \
        {v.casefold() for v in vocabulary}
    allowed_digits = set(re.findall(r"\d+", corpus))
    flags: list[str] = []
    for sentence in _SENTENCE_SPLIT.split(text):
        for i, m in enumerate(_TOKEN.finditer(sentence)):
            token = m.group().rstrip(".'’")
            digits = re.findall(r"\d+", token)
            if digits:
                if any(d not in allowed_digits for d in digits):
                    flags.append(token)
            elif i > 0 and token[:1].isupper() and token.casefold() not in allowed_words:
                flags.append(token)
    return list(dict.fromkeys(flags))


def make_drafter(deps: Deps) -> Node:
    def drafter(state: ParentState) -> Command:
        run_id = state["run_id"]
        db = deps.runs_db
        plan = db.plan_model(run_id)
        assert plan is not None and plan.status == "frozen"
        template, version = load_prompt("drafter")
        items = {i.id: i.text for i in plan.checklist.items}
        signature = effective_signature(plan.sender, deps.profile)
        ctx = LLMContext(runs_db=db, settings=deps.settings, run_id=run_id, prompt_version=version, ids=deps.ids)
        for lead_id in db.draftable_leads(run_id, deps.memory_db.is_blocked_or_contacted):
            q = db.qualification(run_id, lead_id)
            assert q is not None
            company = db.judge_facts(run_id, lead_id).get("company_name") or lead_id
            quotes = [r["quote"] for r in db.evidence(run_id, lead_id)]
            verdicts = json.loads(q["verdicts_json"] or "[]")
            verdict_lines = "\n".join(f"- {items.get(v.get('item_id'), v.get('item_id'))}: {v.get('value')}"
                                      for v in verdicts)
            greeting = f"Hi {q['greeting_name']}," if q["greeting_name"] else "Hi there,"
            prompt = template.format(
                industry=deps.profile.industry, greeting=greeting, call_to_action=plan.sender.call_to_action,
                tone=plan.sender.tone, max_words=plan.sender.max_words, sender_name=plan.sender.name,
                business_name=plan.sender.business_name, offer_text=plan.sender.offer_text, company_name=company,
                lead_id=lead_id, city=plan.city, verdicts=verdict_lines or "- none",
                evidence="\n".join(f"- {qt}" for qt in quotes) or "- none")
            out = structured_call(deps.llms["drafter"], DraftOut, [HumanMessage(content=prompt)], "drafter",
                                  ctx=ctx)
            model = str(getattr(deps.llms["drafter"], "model", "")) or None
            if out is None:
                db.insert_draft(draft_id=deps.ids(), run_id=run_id, lead_id=lead_id, subject="", body="",
                                claim_flags=[], model=model, prompt_version=version, status="draft_failed")
                status = "draft_failed"
            else:
                subject, body = out.subject.strip(), f"{out.body.rstrip()}\n\n{signature}"
                allowed = [*quotes, company, plan.city or "", plan.sender.model_dump_json(), plan.sender.offer_text,
                           greeting, signature, lead_id]
                flags = check_claims(f"{subject}\n{out.body}", allowed, deps.profile.vocabulary)
                db.insert_draft(draft_id=deps.ids(), run_id=run_id, lead_id=lead_id, subject=subject, body=body,
                                claim_flags=flags, model=model, prompt_version=version)
                status = "ok"
            emit(ProgressEvent(kind="draft", run_id=run_id, lead_id=lead_id, status=status))
        return Command(goto="review_next")

    return drafter


def make_send_next(deps: Deps) -> Node:
    """Sends one draft per step. The guard checks run in the design's order, whatever led here."""

    def send_next(state: ParentState) -> Command:
        run_id = state["run_id"]
        db, settings = deps.runs_db, deps.settings
        draft = db.next_sendable(run_id)
        if draft is None:
            return Command(goto="supervisor")
        draft_id, lead_id = draft["draft_id"], draft["lead_id"]
        review = db.final_review(draft_id)
        if review is not None and review["action"] in APPROVED_ACTIONS:
            subject, body, approved_hash = review["final_subject"], review["final_body"], review["approved_hash"]
        else:
            review, subject, body, approved_hash = None, draft["subject"], draft["body"], None
        recipient = settings.mail_test_recipient

        reason = None
        if review is None:
            reason = "not_approved"  # 1
        elif text_hash(subject, body) != approved_hash:
            reason = "hash_mismatch"  # 2
        elif deps.memory_db.is_blocked_or_contacted(lead_id):
            reason = "contacted_or_blocked"  # 3
        elif recipient != settings.mail_test_recipient:
            reason = "recipient_not_test_address"  # 4
        elif settings.mail_smtp_host not in settings.mail_host_allowlist:
            reason = "smtp_host_not_allowed"  # 5
        if reason is not None:
            db.send_block(draft_id, run_id, lead_id, reason)
            emit(ProgressEvent(kind="send", run_id=run_id, lead_id=lead_id, status=f"blocked:{reason}"))
            return Command(goto="send_next")

        q = db.qualification(run_id, lead_id)
        db.send_begin(draft_id, run_id, lead_id)
        deps.memory_db.mark_contacted(lead_id, run_id, draft_id)
        headers = {
            "From": settings.mail_from,
            "To": recipient,
            "X-Intended-Recipient": (q["email"] if q is not None else None) or "",
            "Message-ID": f"<draft-{draft_id}@outreach-bot.local>",
        }
        try:
            message_id = deps.mailer.send(recipient, subject, body, headers)
        except Exception as e:  # SMTP errors: recorded, never retried in v1
            db.send_complete(draft_id, SendStatus.failed, reason=f"{type(e).__name__}: {e}"[:500])
            emit(ProgressEvent(kind="send", run_id=run_id, lead_id=lead_id, status="failed"))
            return Command(goto="send_next")
        db.send_complete(draft_id, SendStatus.sent, message_id=message_id)
        emit(ProgressEvent(kind="send", run_id=run_id, lead_id=lead_id, status="sent"))
        return Command(goto="send_next")

    return send_next
