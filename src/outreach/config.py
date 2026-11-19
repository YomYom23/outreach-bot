"""Loads `Settings` and `TargetProfile` from `config/*.toml` and `.env`.

Files are looked up under `OUTREACH_HOME`, or the repo root found from the package path.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

CALL_SITES = ("parse", "intake_reply", "discovery", "triage", "judge", "checklist", "router", "drafter")
Effort = Literal["low", "medium", "high", "xhigh", "max"]

REQUIRED_ENV = ("ANTHROPIC_API_KEY", "EXA_API_KEY", "MAILTRAP_SMTP_USER", "MAILTRAP_SMTP_PASS")

EXIT_CONFIG_ERROR = 3


class ConfigError(Exception):
    """Configuration is missing or invalid. The CLI exits with code 3."""

    exit_code = EXIT_CONFIG_ERROR


def _default_effort() -> dict[str, Effort]:
    return {site: "medium" for site in CALL_SITES}


class ModelPrice(BaseModel):
    model_config = ConfigDict(frozen=True)
    input: float
    output: float


class Settings(BaseModel):
    """Every limit and switch. Group names in settings.toml are flattened away."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # run limits
    target_qualified: int = 4
    max_research_rounds: int = 3
    batch_size: int = 10
    max_companies_examined_per_round: int = 30
    max_decisions: int = 20
    max_questions: int = 3
    max_exa_searches_per_round: int = 8
    exa_num_results: int = 10
    max_hunter_calls_per_run: int = 5
    rejected_retry_days: int = 90
    # fetching
    extra_pages_per_company: int = 3
    page_text_chars: int = 6000
    snippet_chars: int = 200
    context_snippet_chars: int = 200
    max_redirects: int = 5
    connect_timeout_s: float = 5
    read_timeout_s: float = 10
    max_page_bytes: int = 2_000_000
    vendor_retry_attempts: int = 3
    per_host_delay_s: float = 1.0
    js_shell_min_chars: int = 500
    no_ai_min_ok_pages: int = 2
    user_agent: str = "OutreachAgent/0.1 (+https://github.com/YomYom23/outreach-agent)"
    # models
    model_bulk: str = "claude-haiku-5-5"
    model_quality: str = "claude-sonnet-5-5"
    llm_validation_retries: int = 1
    anthropic_max_retries: int = 2
    effort: dict[str, Effort] = Field(default_factory=lambda: _default_effort())
    max_tokens: dict[str, int] = Field(default_factory=lambda: dict.fromkeys(CALL_SITES, 8000))
    model_prices: dict[str, ModelPrice] = Field(default_factory=dict)
    exa_dollars_per_call: float = 0.022
    hunter_credits_per_call: int = 1
    # mail
    mail_smtp_host: str = "sandbox.smtp.mailtrap.io"
    mail_smtp_port: int = 2525
    mail_starttls: bool = True
    mail_host_allowlist: tuple[str, ...] = ("sandbox.smtp.mailtrap.io",)
    mail_test_recipient: str = "inbox@outreach.test"
    mail_from: str = "outreach-bot@outreach.test"
    mail_min_send_interval_s: float = 10
    # graph
    recursion_margin: int = 50
    steps_per_company: int = 6
    steps_per_draft: int = 3
    outbox_passes: int = 3
    # editor
    editor: str = ""
    editor_early_return_s: float = 2
    # paths
    runs_db_path: Path = Path("runs.db")
    memory_db_path: Path = Path("memory.db")
    # other
    default_router: Literal["claude", "code"] = "claude"
    testing: bool = False

    def model_for(self, call_site: str) -> str:
        return self.model_quality if call_site in ("router", "drafter") else self.model_bulk


class ChecklistItemSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    text: str
    check: Literal["code", "llm"]
    code_check: str | None = None


class DefaultChecklist(BaseModel):
    model_config = ConfigDict(frozen=True)
    threshold: int
    items: tuple[ChecklistItemSpec, ...]


class AiToolMarker(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    html_pattern: str


class SenderDefaults(BaseModel):
    model_config = ConfigDict(frozen=True)
    offer_text: str
    tone: str
    max_words: int
    signature_template: str


class TargetProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    industry: str
    query_templates: tuple[str, ...]
    retry_query_templates: tuple[str, ...]
    directory_blocklist: tuple[str, ...]
    shared_host_patterns: tuple[str, ...]
    ai_tool_markers: tuple[AiToolMarker, ...]
    link_keywords: tuple[str, ...]
    code_checks: tuple[str, ...]
    code_check_phrases: dict[str, tuple[str, ...]]
    default_checklist: DefaultChecklist
    ownership_words: tuple[str, ...]
    role_local_parts: tuple[str, ...]
    placeholder_addresses: tuple[str, ...]
    vendor_denylist: tuple[str, ...]
    file_extension_tlds: tuple[str, ...]
    vocabulary: tuple[str, ...]
    question_templates: dict[str, str]
    sender: SenderDefaults
    fallback_cities: tuple[str, ...] = ()


class Secrets(BaseModel):
    """API keys read from the environment. Never logged."""

    model_config = ConfigDict(frozen=True)
    anthropic_api_key: str = ""
    exa_api_key: str = ""
    hunter_api_key: str = ""
    mailtrap_smtp_user: str = ""
    mailtrap_smtp_pass: str = ""
    langsmith_api_key: str = ""


def find_home() -> Path:
    env = os.environ.get("OUTREACH_HOME")
    if env:
        return Path(env)
    # src/outreach/config.py -> repo root
    return Path(__file__).resolve().parents[2]


def _flatten_settings(raw: dict[str, Any]) -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for group, values in raw.items():
        if group == "effort":
            flat["effort"] = values
        elif group == "max_tokens":
            flat["max_tokens"] = values
        elif group == "prices":
            prices = dict(values)
            flat["model_prices"] = prices.pop("models", {})
            flat.update(prices)
        elif isinstance(values, dict):
            flat.update(values)
        else:
            flat[group] = values
    for key in ("runs_db_path", "memory_db_path"):
        if key in flat:
            flat[key] = Path(os.path.expandvars(str(flat[key])))
    if "mail_host_allowlist" in flat:
        flat["mail_host_allowlist"] = tuple(flat["mail_host_allowlist"])
    return flat


def _profile_from_raw(raw: dict[str, Any], fallback_cities: tuple[str, ...]) -> TargetProfile:
    data = dict(raw)
    data.update(data.pop("lists", {}))
    return TargetProfile.model_validate({**data, "fallback_cities": fallback_cities})


def validate(settings: Settings, secrets: Secrets | None = None) -> None:
    """Raises one `ConfigError` that lists every problem."""
    problems: list[str] = []
    if settings.mail_smtp_host not in settings.mail_host_allowlist:
        problems.append(f"mail_smtp_host {settings.mail_smtp_host!r} is not in mail_host_allowlist")
    if "<you>" in settings.user_agent:
        problems.append("user_agent still contains <you>; set your GitHub user in config/settings.toml")
    missing_sites = [s for s in CALL_SITES if s not in settings.effort or s not in settings.max_tokens]
    if missing_sites:
        problems.append(f"[effort]/[max_tokens] missing call sites: {', '.join(missing_sites)}")
    for model in {settings.model_bulk, settings.model_quality}:
        if model not in settings.model_prices:
            problems.append(f"[prices.models] has no entry for {model}")
    if secrets is not None:
        for env_name in REQUIRED_ENV:
            if not getattr(secrets, env_name.lower()):
                problems.append(f"{env_name} is not set in .env")
    if problems:
        raise ConfigError("Configuration problems:\n- " + "\n- ".join(problems))


def load(home: Path | None = None, *, check_secrets: bool = True) -> tuple[Settings, TargetProfile, Secrets]:
    home = home or find_home()
    load_dotenv(home / ".env", override=False)
    try:
        with open(home / "config" / "settings.toml", "rb") as f:
            settings = Settings.model_validate(_flatten_settings(tomllib.load(f)))
        with open(home / "config" / "target_roofing.toml", "rb") as f:
            profile_raw = tomllib.load(f)
    except FileNotFoundError as e:
        raise ConfigError(f"Missing config file: {e.filename}") from e
    cities_path = home / "config" / "fallback_cities.txt"
    cities = tuple(
        line.strip() for line in cities_path.read_text(encoding="utf-8").splitlines() if line.strip()
    ) if cities_path.exists() else ()
    profile = _profile_from_raw(profile_raw, cities)
    secrets = Secrets(**{
        name.lower(): os.environ.get(name, "")
        for name in (*REQUIRED_ENV, "HUNTER_API_KEY", "LANGSMITH_API_KEY")
    })
    validate(settings, secrets if check_secrets else None)
    for path in (settings.runs_db_path, settings.memory_db_path):
        path.parent.mkdir(parents=True, exist_ok=True)
    return settings, profile, secrets
