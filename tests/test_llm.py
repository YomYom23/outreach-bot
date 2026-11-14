"""Phase 2 exit test: structured_call (refusal and max_tokens -> None, retry, cache, 401)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from pydantic import BaseModel

from outreach.clients import VendorAuthError, VendorUnavailableError
from outreach.config import Settings
from outreach.db.runs_db import RunsDB
from outreach.llm import LLMContext, structured_call
from tests.fakes import FakeChat, SeqIds, invalid, max_tokens, ok, raises, refusal


class Label(BaseModel):
    label: str
    confidence: float


MSGS = [HumanMessage(content="classify acme-roofing.test")]


@pytest.fixture
def ctx(runs_db: RunsDB, settings: Settings) -> LLMContext:
    return LLMContext(runs_db=runs_db, settings=settings, run_id="R1", prompt_version="v1", ids=SeqIds())


def calls(runs_db: RunsDB) -> list[dict[str, Any]]:
    return [dict(r) for r in runs_db.api_calls("R1")]


def test_success_returns_model_and_records_cost(ctx: LLMContext, runs_db: RunsDB) -> None:
    chat = FakeChat([ok({"label": "directory", "confidence": 0.9})])
    result = structured_call(chat, Label, MSGS, "triage", ctx=ctx)
    assert result == Label(label="directory", confidence=0.9)
    [row] = calls(runs_db)
    assert (row["vendor"], row["call_site"], row["model"], row["stop_reason"]) == (
        "anthropic", "triage", "claude-haiku-5-5", "end_turn")
    assert row["cost_estimate"] == pytest.approx((100 * 0.10 + 20 * 0.50) / 1e6)


@pytest.mark.parametrize("step", [refusal(), max_tokens()], ids=["refusal", "max_tokens"])
def test_refusal_and_max_tokens_return_none_without_retry(ctx: LLMContext, runs_db: RunsDB, step: Any) -> None:
    chat = FakeChat([step, ok({"label": "x", "confidence": 1})])
    assert structured_call(chat, Label, MSGS, "judge", ctx=ctx) is None
    assert chat.call_count == 1
    [row] = calls(runs_db)
    assert row["stop_reason"] == step.kind
    assert runs_db.conn.execute("SELECT COUNT(*) FROM llm_cache").fetchone()[0] == 0


def test_validation_retry_appends_error_then_succeeds(ctx: LLMContext, runs_db: RunsDB) -> None:
    chat = FakeChat([invalid('{"label": 3}'), ok({"label": "listicle", "confidence": 0.5})])
    result = structured_call(chat, Label, MSGS, "triage", ctx=ctx)
    assert result is not None and result.label == "listicle"
    assert chat.call_count == 2
    second = chat.calls[1]
    assert len(second) == len(MSGS) + 2  # original + failed reply + correction
    assert "did not match the required JSON schema" in second[-1].content
    assert len(calls(runs_db)) == 2


def test_validation_still_failing_after_one_retry_returns_none(ctx: LLMContext) -> None:
    chat = FakeChat([invalid(), invalid(), ok({"label": "never", "confidence": 0})])
    assert structured_call(chat, Label, MSGS, "checklist", ctx=ctx) is None
    assert chat.call_count == 2  # llm_validation_retries = 1


def test_cache_hit_makes_no_model_call(ctx: LLMContext, runs_db: RunsDB) -> None:
    chat = FakeChat([ok({"label": "directory", "confidence": 0.9})])
    first = structured_call(chat, Label, MSGS, "triage", ctx=ctx)
    second = structured_call(chat, Label, MSGS, "triage", ctx=ctx)
    assert first == second
    assert chat.call_count == 1
    assert len(calls(runs_db)) == 1


def test_cache_key_separates_prompt_version_and_call_site(ctx: LLMContext) -> None:
    chat = FakeChat([ok({"label": "a", "confidence": 1}), ok({"label": "b", "confidence": 1}),
                     ok({"label": "c", "confidence": 1})])
    structured_call(chat, Label, MSGS, "triage", ctx=ctx)
    structured_call(chat, Label, MSGS, "judge", ctx=ctx)
    ctx.prompt_version = "v2"
    structured_call(chat, Label, MSGS, "triage", ctx=ctx)
    assert chat.call_count == 3


def test_failures_are_not_cached(ctx: LLMContext) -> None:
    chat = FakeChat([refusal(), ok({"label": "x", "confidence": 1})])
    assert structured_call(chat, Label, MSGS, "triage", ctx=ctx) is None
    assert structured_call(chat, Label, MSGS, "triage", ctx=ctx) is not None
    assert chat.call_count == 2


def test_unmapped_errors_propagate(ctx: LLMContext) -> None:
    chat = FakeChat([raises(RuntimeError("boom"))])
    with pytest.raises(RuntimeError, match="boom"):
        structured_call(chat, Label, MSGS, "triage", ctx=ctx)


# --- real ChatAnthropic against a local stub of /v1/messages ----------------------


class _Stub(BaseHTTPRequestHandler):
    replies: list[tuple[int, dict]] = []
    requests: list[dict] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).requests.append(body)
        status, payload = type(self).replies.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


def message(stop_reason: str, text: str) -> dict:
    msg: dict[str, Any] = {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 50, "output_tokens": 10},
    }
    if stop_reason == "refusal":
        msg["stop_details"] = {"type": "refusal", "category": "cyber", "explanation": "x"}
    return msg


def error(status: int, kind: str) -> tuple[int, dict]:
    return status, {"type": "error", "error": {"type": kind, "message": kind}}


@pytest.fixture
def stub() -> Iterator[str]:
    _Stub.replies, _Stub.requests = [], []
    server = HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def real_chat(base_url: str, model: str = "claude-haiku-5-5") -> Any:
    from langchain_anthropic import ChatAnthropic
    return ChatAnthropic(model=model, api_key="test-key", base_url=base_url, max_tokens=500,
                         effort="low", max_retries=0)


def test_real_chat_json_schema_request_and_success(stub: str, ctx: LLMContext) -> None:
    _Stub.replies = [(200, message("end_turn", '{"label": "directory", "confidence": 0.8}'))]
    result = structured_call(real_chat(stub, "claude-opus-5-5"), Label, MSGS, "router", ctx=ctx)
    assert result == Label(label="directory", confidence=0.8)
    [req] = _Stub.requests
    assert "tool_choice" not in req
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["output_config"]["effort"] == "low"


def test_real_chat_refusal_returns_none(stub: str, ctx: LLMContext, runs_db: RunsDB) -> None:
    _Stub.replies = [(200, message("refusal", ""))]
    assert structured_call(real_chat(stub), Label, MSGS, "judge", ctx=ctx) is None
    assert [r["stop_reason"] for r in calls(runs_db)] == ["refusal"]


@pytest.mark.parametrize("status", [401, 402])
def test_auth_errors_map_to_vendor_auth_error(stub: str, ctx: LLMContext, status: int) -> None:
    _Stub.replies = [error(status, "authentication_error")]
    with pytest.raises(VendorAuthError) as info:
        structured_call(real_chat(stub), Label, MSGS, "triage", ctx=ctx)
    assert info.value.status == status
    assert "ANTHROPIC_API_KEY" in str(info.value)


@pytest.mark.parametrize("status", [429, 500, 529])
def test_exhausted_retryable_errors_map_to_unavailable(stub: str, ctx: LLMContext, status: int) -> None:
    _Stub.replies = [error(status, "overloaded_error")]
    with pytest.raises(VendorUnavailableError, match="Anthropic"):
        structured_call(real_chat(stub), Label, MSGS, "triage", ctx=ctx)


def test_bad_request_is_not_mapped(stub: str, ctx: LLMContext) -> None:
    import anthropic
    _Stub.replies = [error(400, "invalid_request_error")]
    with pytest.raises(anthropic.BadRequestError):
        structured_call(real_chat(stub), Label, MSGS, "triage", ctx=ctx)
