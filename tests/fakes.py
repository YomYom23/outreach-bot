"""Test doubles. Everything here runs offline.

FakeExa, FakeFetcher and FakeHunter are scripted response queues with call counters; their method
names follow the design (items 31, 44, 45) and are finalized when the real clients land (phases 3b-5).
"""

from __future__ import annotations

import json
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from langchain_core.messages import AIMessage
from pydantic import BaseModel, ValidationError

from outreach.clients import HunterQuotaError, VendorAuthError, VendorUnavailableError

# --- clock -------------------------------------------------------------------


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 11, 13, 12, 0, tzinfo=UTC)
        self._mono = 0.0
        self.slept: list[float] = []

    def now(self) -> datetime:
        return self._now

    def monotonic(self) -> float:
        return self._mono

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.advance(seconds=seconds)

    def advance(self, **kwargs: float) -> None:
        delta = timedelta(**kwargs)
        self._now += delta
        self._mono += delta.total_seconds()


class SeqIds:
    """Deterministic ids: id-0001, id-0002, ..."""

    def __init__(self, prefix: str = "id") -> None:
        self.prefix, self.n = prefix, 0

    def __call__(self) -> str:
        self.n += 1
        return f"{self.prefix}-{self.n:04d}"


# --- Claude ------------------------------------------------------------------


@dataclass
class Step:
    """One scripted model reply.

    kind: "ok" (data = model instance or dict), "invalid" (data = raw text that fails validation),
    "refusal", "max_tokens", "raise" (data = exception), "tool_calls" (data = list of tool calls).
    """

    kind: str
    data: Any = None
    input_tokens: int = 100
    output_tokens: int = 20


def ok(data: Any) -> Step:
    return Step("ok", data)


def invalid(text: str = '{"oops": true}') -> Step:
    return Step("invalid", text)


def refusal() -> Step:
    return Step("refusal")


def max_tokens() -> Step:
    return Step("max_tokens", '{"label": "dir')


def raises(exc: Exception) -> Step:
    return Step("raise", exc)


class FakeChat:
    """Stands in for ChatAnthropic at one call site. Each invoke consumes one Step."""

    def __init__(self, steps: list[Step] | None = None, *, model: str = "claude-haiku-5-5",
                 call_site: str = "test", responder: Callable[[list[Any]], Any] | None = None) -> None:
        """`responder(messages)` builds an `ok` reply whenever no scripted step is left."""
        self.model = model
        self.call_site = call_site
        self.steps: deque[Step] = deque(steps or [])
        self.responder = responder
        self.calls: list[list[Any]] = []  # the messages sent on each call
        self.bound_tools: list[Any] = []

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def _next(self, messages: list[Any]) -> Step:
        self.calls.append(list(messages))
        if not self.steps and self.responder is not None:
            return ok(self.responder(list(messages)))
        if not self.steps:
            raise AssertionError(f"FakeChat[{self.call_site}] has no scripted step left")
        step = self.steps.popleft()
        if step.kind == "raise":
            raise step.data
        return step

    @staticmethod
    def _raw(step: Step, text: str, stop_reason: str, tool_calls: list | None = None) -> AIMessage:
        meta: dict[str, Any] = {"stop_reason": stop_reason}
        if stop_reason == "refusal":
            meta["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "scripted"}
        return AIMessage(content=text, tool_calls=tool_calls or [], response_metadata=meta,
                         usage_metadata={"input_tokens": step.input_tokens, "output_tokens": step.output_tokens,
                                         "total_tokens": step.input_tokens + step.output_tokens})

    def with_structured_output(self, schema: type[BaseModel], *, method: str = "function_calling",
                               include_raw: bool = False, **_: Any) -> _StructuredRunnable:
        assert method == "json_schema", "the design requires method='json_schema'"
        assert include_raw, "the design requires include_raw=True"
        return _StructuredRunnable(self, schema)

    def bind_tools(self, tools: list[Any], **_: Any) -> FakeChat:
        self.bound_tools = tools
        return self

    def invoke(self, messages: list[Any], config: Any = None) -> AIMessage:
        step = self._next(messages)
        if step.kind == "tool_calls":
            return self._raw(step, "", "tool_use", step.data)
        if step.kind in ("refusal", "max_tokens"):
            return self._raw(step, step.data or "", step.kind)
        return self._raw(step, step.data if isinstance(step.data, str) else "done", "end_turn")


class _StructuredRunnable:
    def __init__(self, chat: FakeChat, schema: type[BaseModel]) -> None:
        self.chat, self.schema = chat, schema

    def invoke(self, messages: list[Any], config: Any = None) -> dict[str, Any]:
        step = self.chat._next(messages)
        if step.kind == "ok":
            data = step.data if isinstance(step.data, BaseModel) else self.schema.model_validate(step.data)
            return {"raw": self.chat._raw(step, data.model_dump_json(), "end_turn"), "parsed": data,
                    "parsing_error": None}
        if step.kind == "invalid":
            try:
                self.schema.model_validate_json(step.data)
                raise AssertionError("scripted invalid output actually validates")
            except ValidationError as e:
                err: Exception = e
            return {"raw": self.chat._raw(step, step.data, "end_turn"), "parsed": None, "parsing_error": err}
        if step.kind in ("refusal", "max_tokens"):
            return {"raw": self.chat._raw(step, step.data or "", step.kind), "parsed": None,
                    "parsing_error": ValueError(step.kind)}
        raise AssertionError(f"unsupported step for structured output: {step.kind}")


# --- other vendors -------------------------------------------------------------


@dataclass
class FakeExa:
    """search(query, num_results) -> {"results": [...], "cost_dollars": {"total": ...}}."""

    responses: deque = field(default_factory=deque)
    calls: list[tuple[str, int]] = field(default_factory=list)
    runs_db: Any = None  # when set, records api_calls like ExaClient (the end screen counts searches from them)

    def search(self, query: str, num_results: int, *, run_id: str | None = None, call_site: str = "discovery",
               **_: Any) -> dict:
        self.calls.append((query, num_results))
        if not self.responses:
            out = {"results": [], "cost_dollars": {"total": 0.0}}
        else:
            nxt = self.responses.popleft()
            if nxt == 401:
                raise VendorAuthError("Exa", 401, "EXA_API_KEY")
            if nxt == 429:
                raise VendorUnavailableError("Exa", "HTTP 429")
            out = dict(nxt)
        if self.runs_db is not None:
            self.runs_db.record_api_call(call_id=f"exa-{len(self.calls)}", run_id=run_id, vendor="exa",
                                         call_site=call_site, units=1, cost_estimate=out["cost_dollars"]["total"])
        return out


@dataclass
class FetchResult:
    url: str
    status: int = 200
    html: str = ""
    final_url: str | None = None
    content_type: str = "text/html"
    error: str | None = None  # "timeout", "private_ip", ...


@dataclass
class FakeFetcher:
    """fetch(domain, path) -> FetchResult, from a {url: FetchResult} map."""

    pages: dict[str, FetchResult] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def fetch(self, domain: str, path: str = "/") -> FetchResult:
        url = f"https://{domain}{path}"
        self.calls.append(url)
        return self.pages.get(url, FetchResult(url=url, status=404, error="http_error"))


@dataclass
class FakeHunter:
    """domain_search(domain) -> Hunter JSON; account() -> credits; 'quota' raises HunterQuotaError."""

    by_domain: dict[str, Any] = field(default_factory=dict)
    credits: int = 25
    calls: Counter = field(default_factory=Counter)
    runs_db: Any = None  # when set, records api_calls like HunterClient (the per-run cap counts them)

    def domain_search(self, domain: str, *, run_id: str | None = None, **_: Any) -> dict:
        self.calls["domain_search"] += 1
        result = self.by_domain.get(domain, {"data": {"domain": domain, "emails": []}})
        if result == "quota":
            raise HunterQuotaError(domain)
        if self.runs_db is not None:
            self.runs_db.record_api_call(call_id=f"hunter-{self.calls['domain_search']}", run_id=run_id,
                                         vendor="hunter", call_site="domain_search", units=1)
        return json.loads(json.dumps(result))

    def credits_remaining(self) -> int:
        return int(self.account()["data"]["requests"]["credits"]["remaining"])

    def account(self) -> dict:
        self.calls["account"] += 1
        return {"data": {"requests": {"credits": {"used": 0, "available": self.credits,
                                                  "remaining": self.credits}}}}


@dataclass
class SentMail:
    recipient: str
    subject: str
    body: str
    headers: dict[str, str]


@dataclass
class RecordingMailer:
    sent: list[SentMail] = field(default_factory=list)
    fail_with: Exception | None = None

    def send(self, recipient: str, subject: str, body: str, headers: dict[str, str]) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(SentMail(recipient, subject, body, dict(headers)))
        return headers.get("Message-ID", f"<fake-{len(self.sent)}@outreach.test>")
