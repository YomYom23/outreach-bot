"""Claude calls: one ChatAnthropic per call site, and the `structured_call` helper.

`structured_call` returns the parsed model, or None on a refusal, a `max_tokens` stop, or when the
validation retry runs out. It reads through `llm_cache` and records every vendor call in `api_calls`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib import resources
from typing import TYPE_CHECKING, Any, cast

import anthropic
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, messages_to_dict
from pydantic import BaseModel, SecretStr

from outreach.clients import VendorAuthError, VendorUnavailableError
from outreach.config import CALL_SITES

if TYPE_CHECKING:
    from langchain_anthropic import ChatAnthropic

    from outreach.config import Secrets, Settings
    from outreach.db.runs_db import RunsDB

NO_RETRY_STOP_REASONS = ("refusal", "max_tokens")


def load_prompt(name: str) -> tuple[str, str]:
    """Returns (text, prompt_version) for `prompts/<name>.md`. The version is the file's sha256."""
    text = resources.files("outreach").joinpath("prompts", f"{name}.md").read_text(encoding="utf-8")
    return text, hashlib.sha256(text.encode()).hexdigest()


def build_llms(settings: Settings, secrets: Secrets) -> dict[str, ChatAnthropic]:
    """One instance per call site, each with its own model, effort, max_tokens and retries.

    `thinking` is never set: Opus 5.5 can't disable it, and effort is the only control
    (docs/vendor_notes.md).
    """
    from langchain_anthropic import ChatAnthropic

    # Pydantic field aliases (model, max_tokens, api_key) aren't visible to mypy.
    return {
        site: ChatAnthropic(  # type: ignore[call-arg]
            model=settings.model_for(site),
            effort=settings.effort[site],
            max_tokens=settings.max_tokens[site],
            max_retries=settings.anthropic_max_retries,
            api_key=SecretStr(secrets.anthropic_api_key),
        )
        for site in CALL_SITES
    }


@dataclass
class LLMContext:
    """Where a call is recorded."""

    runs_db: RunsDB
    settings: Settings
    run_id: str | None
    prompt_version: str
    ids: Callable[[], str]
    config: dict | None = None  # RunnableConfig passed to invoke (tracing metadata)


def cache_key(call_site: str, model: str, prompt_version: str, schema: type[BaseModel],
              messages: Sequence[BaseMessage]) -> str:
    payload = json.dumps({
        "call_site": call_site, "model": model, "prompt_version": prompt_version,
        "schema": schema.model_json_schema(), "messages": messages_to_dict(list(messages)),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _model_name(llm: Any) -> str:
    return str(getattr(llm, "model", None) or getattr(llm, "model_name", "unknown"))


def _usage(raw: AIMessage) -> tuple[int | None, int | None, int | None]:
    usage = getattr(raw, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    return usage.get("input_tokens"), usage.get("output_tokens"), details.get("cache_read")


def _cost(settings: Settings, model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    price = settings.model_prices.get(model)
    if price is None or input_tokens is None or output_tokens is None:
        return None
    return (input_tokens * price.input + output_tokens * price.output) / 1_000_000


def _map_anthropic_error(exc: Exception) -> Exception | None:
    """Maps SDK errors after ChatAnthropic's own retries are spent."""
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code in (401, 402):
            return VendorAuthError("Anthropic", exc.status_code, "ANTHROPIC_API_KEY")
        if exc.status_code == 429 or exc.status_code >= 500:
            return VendorUnavailableError("Anthropic", f"HTTP {exc.status_code}")
        return None
    if isinstance(exc, anthropic.APIConnectionError):  # includes APITimeoutError
        return VendorUnavailableError("Anthropic", type(exc).__name__)
    return None


def chat_call(llm: Any, messages: Sequence[BaseMessage], call_site: str, *, ctx: LLMContext) -> AIMessage:
    """A plain (tool-calling) model turn: maps vendor errors and records `api_calls`. Not cached."""
    try:
        raw: AIMessage = llm.invoke(list(messages), config=ctx.config) if ctx.config else llm.invoke(list(messages))
    except Exception as exc:
        mapped = _map_anthropic_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise
    model = _model_name(llm)
    input_tokens, output_tokens, cached_tokens = _usage(raw)
    ctx.runs_db.record_api_call(
        call_id=ctx.ids(), run_id=ctx.run_id, vendor="anthropic", call_site=call_site, model=model,
        input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
        stop_reason=(raw.response_metadata or {}).get("stop_reason"),
        cost_estimate=_cost(ctx.settings, model, input_tokens, output_tokens))
    return raw


def structured_call[M: BaseModel](llm: Any, schema: type[M], messages: Sequence[BaseMessage], call_site: str,
                                  *, ctx: LLMContext) -> M | None:
    model = _model_name(llm)
    key = cache_key(call_site, model, ctx.prompt_version, schema, messages)
    cached = ctx.runs_db.llm_cache_get(key)
    if cached is not None:
        return schema.model_validate_json(cached)

    runnable = llm.with_structured_output(schema, method="json_schema", include_raw=True)
    convo: list[BaseMessage] = list(messages)
    attempts = 1 + ctx.settings.llm_validation_retries
    for attempt in range(attempts):
        try:
            result = runnable.invoke(convo, config=ctx.config) if ctx.config else runnable.invoke(convo)
        except Exception as exc:
            mapped = _map_anthropic_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
        raw: AIMessage = result["raw"]
        stop_reason = (raw.response_metadata or {}).get("stop_reason")
        input_tokens, output_tokens, cached_tokens = _usage(raw)
        ctx.runs_db.record_api_call(
            call_id=ctx.ids(), run_id=ctx.run_id, vendor="anthropic", call_site=call_site, model=model,
            input_tokens=input_tokens, output_tokens=output_tokens, cached_tokens=cached_tokens,
            stop_reason=stop_reason, cost_estimate=_cost(ctx.settings, model, input_tokens, output_tokens),
        )
        if stop_reason in NO_RETRY_STOP_REASONS:
            return None
        parsed = result.get("parsed")
        if parsed is not None:
            ctx.runs_db.llm_cache_put(key, parsed.model_dump_json())
            return cast(M, parsed)
        if attempt + 1 < attempts:
            error = result.get("parsing_error")
            convo = [*convo, raw, HumanMessage(
                content=f"Your previous reply did not match the required JSON schema: {error}. "
                        "Reply again with only JSON that matches the schema.")]
    return None
