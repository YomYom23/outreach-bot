"""Enums and Pydantic models from the design's Data model section."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class Move(StrEnum):
    research = "research"
    qualify = "qualify"
    ask = "ask"
    draft = "draft"
    finish = "finish"


SUPERVISOR_KEYS = ("extra_round", "change_city", "lower_threshold")
INTAKE_KEYS = ("city", "sender_name", "business_name", "call_to_action")


class QuestionKey(StrEnum):
    extra_round = "extra_round"
    change_city = "change_city"
    lower_threshold = "lower_threshold"
    city = "city"
    sender_name = "sender_name"
    business_name = "business_name"
    call_to_action = "call_to_action"


class StopReason(StrEnum):
    complete = "complete"
    exhausted_rounds = "exhausted_rounds"
    exhausted_empty_round = "exhausted_empty_round"
    decision_cap = "decision_cap"
    no_candidates = "no_candidates"
    user_discarded = "user_discarded"


STOP_REASON_TEXT: dict[StopReason, str] = {
    StopReason.complete: "target reached",
    StopReason.exhausted_rounds: "research rounds used up",
    StopReason.exhausted_empty_round: "a research round found no new companies",
    StopReason.decision_cap: "supervisor decision limit reached",
    StopReason.no_candidates: "no usable companies found",
    StopReason.user_discarded: "discarded",
}


class StageStopReason(StrEnum):
    filled = "filled"
    queue_empty = "queue_empty"
    limit_searches = "limit_searches"
    limit_companies = "limit_companies"
    seeds_done = "seeds_done"
    limit_hunter = "limit_hunter"


class RunStatusValue(StrEnum):
    active = "active"
    paused = "paused"
    failed = "failed"
    complete = "complete"
    discarded = "discarded"


UNFINISHED_RUN_STATUSES = (RunStatusValue.active, RunStatusValue.paused, RunStatusValue.failed)


class ResearchStatus(StrEnum):
    queued = "queued"
    researching = "researching"
    candidate = "candidate"
    parked_no_email = "parked_no_email"
    rejected_triage = "rejected_triage"
    rejected_not_roofer = "rejected_not_roofer"
    rejected_non_us = "rejected_non_us"
    rejected_fetch_failed = "rejected_fetch_failed"
    released = "released"


REJECTED_STATUSES = tuple(s for s in ResearchStatus if s.value.startswith("rejected_"))
COUNTED_STATUSES = (ResearchStatus.candidate, ResearchStatus.parked_no_email)

RESEARCH_TRANSITIONS: dict[ResearchStatus, frozenset[ResearchStatus]] = {
    ResearchStatus.queued: frozenset({ResearchStatus.researching, ResearchStatus.rejected_triage,
                                      ResearchStatus.released}),
    ResearchStatus.researching: frozenset({
        ResearchStatus.candidate, ResearchStatus.parked_no_email, ResearchStatus.rejected_not_roofer,
        ResearchStatus.rejected_non_us, ResearchStatus.rejected_fetch_failed, ResearchStatus.released,
    }),
}


class FetchStatus(StrEnum):
    ok = "ok"
    blocked = "blocked"
    js_shell = "js_shell"
    timeout = "timeout"
    http_error = "http_error"
    offsite_redirect = "offsite_redirect"
    soft_404 = "soft_404"
    private_ip = "private_ip"


class DomainRelation(StrEnum):
    same_domain = "same_domain"
    freemail = "freemail"
    other_domain = "other_domain"


class ExtractMethod(StrEnum):
    visible_text = "visible_text"
    mailto = "mailto"
    jsonld = "jsonld"
    cf_decoded = "cf_decoded"
    text_obfuscated = "text_obfuscated"
    hunter = "hunter"


class VerdictValue(StrEnum):
    met = "met"
    not_met = "not_met"
    unknown = "unknown"


class EmailType(StrEnum):
    owner = "owner"
    general = "general"


class TriageLabel(StrEnum):
    contractor_own_site = "contractor_own_site"
    directory = "directory"
    listicle = "listicle"
    lead_gen = "lead_gen"
    manufacturer_supplier = "manufacturer_supplier"
    franchise_hub = "franchise_hub"
    other = "other"


class ReviewAction(StrEnum):
    approve = "approve"
    edit = "edit"
    reject = "reject"
    skip = "skip"
    back = "back"


class SendStatus(StrEnum):
    sending = "sending"
    sent = "sent"
    failed = "failed"
    blocked = "blocked"
    unknown = "unknown"


class DecidedBy(StrEnum):
    forced = "forced"
    router = "router"
    fallback = "fallback"


# --- plan ---------------------------------------------------------------


class ChecklistItem(BaseModel):
    id: str
    text: str
    check: Literal["code", "llm"]
    code_check: str | None = None


class Checklist(BaseModel):
    key: str = ""
    version: int = 1
    items: list[ChecklistItem]
    threshold: int | None = None

    def threshold_valid(self) -> bool:
        return self.threshold is not None and 1 <= self.threshold <= len(self.items)


class SenderProfile(BaseModel):
    name: str = ""
    business_name: str = ""
    call_to_action: str = ""
    offer_text: str = ""
    tone: str = ""
    max_words: int = 120
    signature: str = ""


class RunPlan(BaseModel):
    run_id: str
    version: int
    status: Literal["draft", "frozen"]
    city: str | None
    target_qualified: int
    checklist: Checklist
    sender: SenderProfile


# --- supervisor ---------------------------------------------------------


class RunStatus(BaseModel):
    """Computed from runs.db each supervisor turn. Holds no page or model text."""

    model_config = ConfigDict(frozen=True)

    unqualified_candidates: int
    qualified_count: int
    near_miss_leads: int
    target_qualified: int
    research_rounds_used: int
    max_research_rounds: int
    last_round_new_companies: int | None
    undrafted_qualified: int
    questions_used: int
    max_questions: int
    decisions_used: int
    max_decisions: int
    answered_keys: frozenset[QuestionKey]
    available_question_keys: frozenset[QuestionKey]
    exhausted_reason: Literal["rounds", "empty_round"] | None
    last_stage_stop_reason: StageStopReason | None
    router: Literal["claude", "code"]

    @field_serializer("answered_keys", "available_question_keys")
    def _sorted_keys(self, keys: frozenset[QuestionKey]) -> list[str]:
        # Sorted so status_json is identical across processes (record_decision dedups on it).
        return sorted(k.value for k in keys)

    @property
    def complete(self) -> bool:
        return self.qualified_count >= self.target_qualified

    @property
    def exhausted(self) -> bool:
        return self.exhausted_reason is not None

    @property
    def ask_available(self) -> bool:
        return (self.router == "claude" and self.questions_used < self.max_questions
                and bool(self.available_question_keys))


class Decision(BaseModel):
    move: Move
    reason: str
    question_key: QuestionKey | None = None
    assumption: str | None = None


class PendingQuestion(BaseModel):
    key: QuestionKey
    origin: Literal["intake", "supervisor"]
    prompt_text: str
    allowed_answers: list[str] | None = None


# --- pauses ---------------------------------------------------------------


class PausePayload(BaseModel):
    kind: Literal["question", "plan", "draft_review"]
    pause_id: str
    prompt_text: str
    allowed_answers: list[str] | None = None
    context: dict = Field(default_factory=dict)


class PlanAnswer(BaseModel):
    kind: Literal["plan"] = "plan"
    pause_id: str
    action: Literal["confirm", "edit"]
    edits: dict[str, str] = Field(default_factory=dict)
    remember: set[Literal["sender", "checklist"]] = Field(default_factory=set)


class QuestionAnswer(BaseModel):
    kind: Literal["question"] = "question"
    pause_id: str
    key: QuestionKey
    value: str
    remember: bool = False


class ReviewAnswer(BaseModel):
    kind: Literal["draft_review"] = "draft_review"
    pause_id: str
    draft_id: str
    shown_hash: str
    action: ReviewAction
    subject: str | None = None
    body: str | None = None


Answer = Annotated[PlanAnswer | QuestionAnswer | ReviewAnswer, Field(discriminator="kind")]


# --- research -----------------------------------------------------------


class Person(BaseModel):
    name: str
    role: str | None = None
    quote: str
    page_id: int | None = None


class Location(BaseModel):
    text: str
    quote: str
    page_id: int | None = None


class JudgeResult(BaseModel):
    is_single_roofer_own_site: bool
    own_site_quote: str | None = None
    company_name: str | None = None
    name_quote: str | None = None
    people: list[Person] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)


class EmailObservation(BaseModel):
    obs_id: str | None = None
    run_id: str
    lead_id: str
    address_norm: str
    address_as_seen: str
    page_id: int | None = None
    source_url: str
    method: ExtractMethod
    domain_relation: DomainRelation
    owner_link: Literal["none", "name_match"] = "none"
    matched_form: str | None = None
    hunter_json: str | None = None


class CandidateV1(BaseModel):
    schema_version: Literal[1] = 1
    lead_id: str
    run_id: str
    round: int
    domain_key: str
    site_url: str
    company_name: str | None = None
    people: list[Person] = Field(default_factory=list)
    locations: list[Location] = Field(default_factory=list)
    us_evidence: str | None = None
    email_observations: list[EmailObservation] = Field(default_factory=list)
    phones: list[str] = Field(default_factory=list)
    contact_form_url: str | None = None
    page_ids: list[int] = Field(default_factory=list)
    research_status: ResearchStatus


class TriageOut(BaseModel):
    """classify_site's structured output (prompts/triage.md)."""

    label: TriageLabel
    confidence: float
    reason: str = ""


class Verdict(BaseModel):
    item_id: str
    value: VerdictValue
    quote: str | None = None
    page_id: int | None = None


class IntakeReply(BaseModel):
    """Is an intake reply an answer, or the user asking something back? (prompts/intake_reply.md)"""

    kind: Literal["answer", "question"]
    reply: str | None = Field(default=None, description="Only for `question`: the help to show them.")


class ChecklistOut(BaseModel):
    """The checklist judge's structured output (prompts/checklist_judge.md)."""

    verdicts: list[Verdict] = Field(default_factory=list)


class QualifiedLead(BaseModel):
    run_id: str
    lead_id: str
    checklist_version: int
    points: int
    verdicts_json: str
    qualified: bool
    email: str | None = None
    email_type: EmailType | None = None
    email_source_kind: Literal["page", "hunter"] | None = None
    email_source_url: str | None = None
    greeting_name: str | None = None
    drop_reason: str | None = None


class Draft(BaseModel):
    draft_id: str
    lead_id: str
    subject: str
    body: str
    text_hash: str
    claim_flags: list[str] = Field(default_factory=list)
    status: Literal["ok", "draft_failed"] = "ok"


class StageResult(BaseModel):
    stage: Literal["research", "qualify"]
    round: int | None = None
    counts: dict[str, int] = Field(default_factory=dict)
    stop_reason: StageStopReason | None = None
    new_ids: list[str] = Field(default_factory=list)


class ProgressEvent(BaseModel):
    kind: str
    run_id: str
    round: int | None = None
    lead_id: str | None = None
    status: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)
