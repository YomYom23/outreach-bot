"""Hunter Domain Search for form-only leads (design item 42; Decisions: Hunter; Error policy).

Runs only for a lead with no on-site (`same_domain`/`freemail`) address whose points are at least N. Reads
through `hunter_cache` first, so a cached domain costs nothing and doesn't count toward the cap. Keeps only
addresses with a source URL, as `email_observations(method='hunter')`. Never calls Email Finder.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from outreach.clients import HunterQuotaError
from outreach.models import ExtractMethod
from outreach.research.extract import domain_relation

if TYPE_CHECKING:
    from outreach.deps import Deps

CALL_SITE = "domain_search"


class HunterOutcome(StrEnum):
    searched = "searched"  # results stored (possibly none)
    cached = "cached"
    cap = "hunter_cap"
    quota = "hunter_quota"
    unavailable = "unavailable"  # no Hunter client configured


@dataclass
class HunterBudget:
    """Remaining credits, read once per qualify pass from `account()` (which costs no credit)."""

    credits: int | None = None
    checked: bool = False


def calls_this_run(deps: Deps, run_id: str) -> int:
    return len(deps.runs_db.api_calls(run_id, call_site=CALL_SITE))


def store_results(deps: Deps, run_id: str, lead_id: str, body: dict) -> int:
    """Writes sourced addresses as observations; returns how many."""
    stored = 0
    for entry in (body.get("data") or {}).get("emails") or []:
        address = (entry.get("value") or "").strip().lower()
        sources = [s for s in entry.get("sources") or [] if s.get("uri")]
        if "@" not in address or not sources:
            continue
        deps.runs_db.add_email_observation(
            obs_id=f"{run_id}:{lead_id}:{address}:hunter", run_id=run_id, lead_id=lead_id, address_norm=address,
            address_as_seen=entry.get("value") or address, page_id=None, source_url=sources[0]["uri"],
            method=ExtractMethod.hunter, domain_relation=domain_relation(address, lead_id),
            hunter_json=json.dumps(entry))
        stored += 1
    return stored


def find_hunter_emails(deps: Deps, run_id: str, lead_id: str, budget: HunterBudget) -> HunterOutcome:
    db = deps.runs_db
    cached = db.hunter_cache_get(lead_id)
    if cached is not None:
        store_results(deps, run_id, lead_id, cached)
        return HunterOutcome.cached
    if deps.hunter is None:
        return HunterOutcome.unavailable
    if calls_this_run(deps, run_id) >= deps.settings.max_hunter_calls_per_run:
        return HunterOutcome.cap
    if not budget.checked:
        budget.credits, budget.checked = deps.hunter.credits_remaining(), True
    if budget.credits is not None and budget.credits <= 0:
        return HunterOutcome.quota
    try:
        body = deps.hunter.domain_search(lead_id, run_id=run_id)
    except HunterQuotaError:
        budget.credits = 0
        return HunterOutcome.quota
    if budget.credits is not None:
        budget.credits -= 1
    db.hunter_cache_put(lead_id, body)
    store_results(deps, run_id, lead_id, body)
    return HunterOutcome.searched
