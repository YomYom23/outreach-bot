"""Discovery (design item 31): the `exa_search` tool the Haiku discovery loop calls.

The model chooses the query and objective. Code does everything else: limits, the search cache, domain
normalization, skipping, and queueing. The tool reads `run_id` and `round` from its injected RunnableConfig,
so the model never sees or sets them.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from outreach.clients.exa_client import OBJECTIVE_DESCRIPTION
from outreach.research.domains import Skip, normalize_domain

if TYPE_CHECKING:
    from outreach.deps import Deps

QUERY_DESCRIPTION = ("A natural-language description of the pages to find, for example "
                     "'roofing contractor in Tucson, AZ'.")
TOOL_DESCRIPTION = ("Search the web for local companies' own websites. Returns the new company domains this search "
                    "found, each with its page title and a short excerpt.")
LIMIT_SEARCHES = "LIMIT: the search limit for this round is reached. Stop searching and reply without a tool call."
LIMIT_COMPANIES = "LIMIT: enough companies are queued for this round. Stop searching and reply without a tool call."


class ExaSearchArgs(BaseModel):
    query: str = Field(description=QUERY_DESCRIPTION)
    objective: str = Field(description=OBJECTIVE_DESCRIPTION)


def request_hash(run_id: str, query: str, objective: str, num_results: int) -> str:
    payload = json.dumps({"run_id": run_id, "query": query.strip().lower(), "objective": objective.strip(),
                          "num_results": num_results}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def snippet(result: dict, chars: int) -> str:
    return " ".join(" ".join(result.get("highlights") or []).split())[:chars]


def round_limits_hit(deps: Deps, run_id: str, round_: int) -> str | None:
    s, db = deps.settings, deps.runs_db
    if len(db.round_rows(run_id, round_)) >= s.max_companies_examined_per_round:
        return LIMIT_COMPANIES
    if db.searches_in_round(run_id, round_) >= s.max_exa_searches_per_round:
        return LIMIT_SEARCHES
    return None


def search_and_queue(deps: Deps, run_id: str, round_: int, query: str, objective: str,
                     checklist_key: str | None) -> str:
    """The tool body. Returns the text the model sees."""
    s, db, profile = deps.settings, deps.runs_db, deps.profile
    key = request_hash(run_id, query, objective, s.exa_num_results)
    response = db.cache_search_get(key)
    if response is None:
        if (limit := round_limits_hit(deps, run_id, round_)) is not None:
            return limit
        response = deps.exa.search(query, s.exa_num_results, objective=objective, run_id=run_id,
                                   call_site="discovery")
        db.cache_search_put(request_hash=key, run_id=run_id, round_=round_, query=query, response=response)
    room = s.max_companies_examined_per_round - len(db.round_rows(run_id, round_))
    rows: list[str] = []
    skipped = 0
    for result in response.get("results", []):
        domain = normalize_domain(result.get("url", ""), shared_host_patterns=profile.shared_host_patterns,
                                  blocklist=profile.directory_blocklist)
        if isinstance(domain, Skip) or deps.memory_db.should_skip(domain, checklist_key):
            skipped += 1
            continue
        if room <= 0 or not db.queue_candidates(run_id, round_, [domain], found_by_query=query):
            continue  # already queued in this run, or the round is full
        room -= 1
        title = " ".join((result.get("title") or "").split())[:120]
        rows.append(f"- {domain} | {title} | {snippet(result, s.snippet_chars)}")
    head = f"{len(rows)} new companies ({skipped} results skipped: directories, shared hosts or already known)."
    tail = [LIMIT_COMPANIES] if room <= 0 else []
    return "\n".join([head, *rows, *tail])


def make_exa_search(deps: Deps) -> StructuredTool:
    def exa_search(query: str, objective: str, config: RunnableConfig) -> str:
        conf: dict[str, Any] = config.get("configurable", {})
        return search_and_queue(deps, conf["run_id"], int(conf["round"]), query, objective,
                                conf.get("checklist_key"))

    return StructuredTool.from_function(exa_search, name="exa_search", description=TOOL_DESCRIPTION,
                                        args_schema=ExaSearchArgs)


def result_index(deps: Deps, run_id: str, round_: int) -> dict[str, tuple[str, str]]:
    """domain -> (title, snippet) from this round's cached searches, first occurrence wins (for triage)."""
    s, profile = deps.settings, deps.profile
    out: dict[str, tuple[str, str]] = {}
    for _query, response in deps.runs_db.search_responses(run_id, round_):
        for result in response.get("results", []):
            domain = normalize_domain(result.get("url", ""), shared_host_patterns=profile.shared_host_patterns,
                                      blocklist=profile.directory_blocklist)
            if isinstance(domain, str) and domain not in out:
                out[domain] = (result.get("title") or "", snippet(result, s.snippet_chars))
    return out
