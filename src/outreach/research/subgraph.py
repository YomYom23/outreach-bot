"""The researcher subgraph and its `run_research` wrapper (design item 30, Subgraph contracts).

The subgraph is compiled with `checkpointer=False`: if the process dies, the wrapper re-runs from its first
line and rebuilds progress from runs.db. Every paid call reads through a cache (search_calls, triage_labels,
pages, llm_cache), so a re-run pays only for work that never finished. Discovery's model turns are the one
exception: they aren't cached, so discovery is skipped once the round has queue rows.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from outreach.graph import Node, emit, invoke_subgraph
from outreach.llm import LLMContext, chat_call, load_prompt
from outreach.models import COUNTED_STATUSES, ProgressEvent, ResearchStatus, StageStopReason, TriageLabel
from outreach.research.discovery import make_exa_search, result_index, round_limits_hit
from outreach.research.domains import normalize_domain
from outreach.research.extract import ExtractConfig, FoundEmail, extract_page
from outreach.research.judge import decide_status, judge_company, verify_quotes
from outreach.research.links import choose_links
from outreach.state import ResearchState
from outreach.tracing import child_config

if TYPE_CHECKING:
    from outreach.deps import Deps

SEED_QUERY = "seed"


def _conf(config: RunnableConfig) -> tuple[str, int, str | None]:
    c = config.get("configurable", {})
    return c["run_id"], int(c["round"]), c.get("checklist_key")


def _run_city(deps: Deps, run_id: str) -> str | None:
    """The run plan's city, recorded on each lead the run claims."""
    plan = deps.runs_db.plan_model(run_id)
    return plan.city if plan is not None else None


def _seeds(deps: Deps, run_id: str) -> list[str]:
    row = deps.runs_db.get_run(run_id)
    return json.loads(row["seeds_json"]) if row is not None and row["seeds_json"] else []


# --- per company ---------------------------------------------------------------------


def research_company(deps: Deps, run_id: str, round_: int, lead_id: str, checklist_key: str | None,
                     config: dict | None = None) -> ResearchStatus:
    """Steps 1-6 of 'Per company'. Every runs.db write is idempotent; memory.db is finalized last."""
    db, memory, s, profile = deps.runs_db, deps.memory_db, deps.settings, deps.profile
    if not memory.claim(lead_id, run_id, checklist_key, city=_run_city(deps, run_id)):
        reason = memory.skip_reason(lead_id, checklist_key)
        db.set_candidate_status(run_id, lead_id, ResearchStatus.released, drop_reason=f"skip:{reason}")
        return ResearchStatus.released
    row = db.candidate(run_id, lead_id)
    if row is not None and row["status"] == ResearchStatus.queued:
        db.set_candidate_status(run_id, lead_id, ResearchStatus.researching)

    home = deps.fetcher.fetch_page(run_id, lead_id, "/")
    pages = [home]
    if home.ok:
        for path in choose_links(home.html, home.final_url or home.requested_url, lead_id, profile.link_keywords,
                                 s.extra_pages_per_company):
            pages.append(deps.fetcher.fetch_page(run_id, lead_id, path))
    ok_pages = [p for p in pages if p.ok]

    cfg = ExtractConfig.from_profile(profile, s)
    emails: list[tuple[FoundEmail, int, str]] = []
    us_evidence: str | None = None
    form_url: str | None = None
    for p in ok_pages:
        found = extract_page(p.html, p.final_url or p.requested_url, lead_id, cfg)
        db.set_page_markers(p.page_id, found.markers)
        emails += [(e, p.page_id, p.final_url or p.requested_url) for e in found.emails]
        for phone in found.phones:
            db.add_phone(run_id, lead_id, phone, p.page_id)
        us_evidence = us_evidence or found.us_evidence
        form_url = form_url or found.contact_form_url

    verified = None
    drop: str | None
    if not ok_pages:
        status, drop = ResearchStatus.rejected_fetch_failed, f"fetch:{home.fetch_status}"
    else:
        result = judge_company(deps, run_id, ok_pages, config)
        verified = verify_quotes(result, ok_pages, s.page_text_chars) if result is not None else None
        status = decide_status(verified, us_evidence, [e for e, _, _ in emails])
        drop = None if status in COUNTED_STATUSES else status.value

    if verified is not None:
        _write_evidence(deps, run_id, lead_id, verified)
    for e, page_id, url in emails:
        db.add_email_observation(obs_id=f"{run_id}:{lead_id}:{e.address_norm}:{e.method}", run_id=run_id,
                                 lead_id=lead_id, address_norm=e.address_norm, address_as_seen=e.address_as_seen,
                                 page_id=page_id, source_url=url, method=e.method,
                                 domain_relation=e.domain_relation)
    judge_json = json.dumps({"judge": verified.model_dump() if verified else None, "contact_form_url": form_url,
                             "ok_pages": len(ok_pages)})
    db.set_candidate_status(run_id, lead_id, status, judge_json=judge_json, us_evidence=us_evidence,
                            drop_reason=drop)
    memory.finalize(lead_id, run_id, status)
    emit(ProgressEvent(kind="research", run_id=run_id, round=round_, lead_id=lead_id, status=status))
    return status


def _write_evidence(deps: Deps, run_id: str, lead_id: str, verified: Any) -> None:
    db = deps.runs_db

    def add(kind: str, i: int, quote: str | None, page_id: int | None = None) -> None:
        if quote:
            db.add_evidence(evidence_id=f"{run_id}:{lead_id}:{kind}:{i}", run_id=run_id, lead_id=lead_id,
                            kind=kind, quote=quote, page_id=page_id)

    add("own_site", 0, verified.own_site_quote)
    add("name", 0, verified.name_quote)
    for i, person in enumerate(verified.people):
        add("person", i, person.quote, person.page_id)
    for i, loc in enumerate(verified.locations):
        add("location", i, loc.quote, loc.page_id)


# --- graph nodes -----------------------------------------------------------------------


def build_researcher(deps: Deps) -> Any:
    tool = make_exa_search(deps)
    db, s, profile = deps.runs_db, deps.settings, deps.profile

    def seed_or_discover(state: ResearchState, config: RunnableConfig) -> Command:
        run_id, round_, checklist_key = _conf(config)
        seeds = _seeds(deps, run_id) if round_ == 1 else []
        if seeds:
            keys = []
            city = _run_city(deps, run_id)
            for seed in seeds:
                key = normalize_domain(seed, shared_host_patterns=profile.shared_host_patterns,
                                       blocklist=profile.directory_blocklist)
                if isinstance(key, str) and deps.memory_db.claim(key, run_id, checklist_key, city=city):
                    keys.append(key)
            db.queue_candidates(run_id, round_, keys, found_by_query=SEED_QUERY)
            return Command(goto="next_company")
        if db.round_has_rows(run_id, round_):
            return Command(goto="triage_results")
        return Command(goto="discover")

    def discover(state: ResearchState, config: RunnableConfig) -> Command:
        run_id, round_, _ = _conf(config)
        messages = list(state.get("discovery_messages") or [])
        model_turns = sum(isinstance(m, AIMessage) for m in messages)
        if round_limits_hit(deps, run_id, round_) or model_turns >= s.max_exa_searches_per_round + 2:
            return Command(goto="triage_results")
        new: list[Any] = []
        if not messages:
            prompt, _v = load_prompt("discovery")
            plan = db.plan_model(run_id)
            templates = profile.query_templates if round_ == 1 else profile.retry_query_templates
            city = plan.city if plan is not None else ""
            text = prompt.format(industry=profile.industry, city=city,
                                 templates="\n".join(f"- {t.format(city=city)}" for t in templates),
                                 tried="\n".join(f"- {q}" for q in db.queries_tried(run_id)) or "(none)")
            new.append(HumanMessage(content=text))
        _p, version = load_prompt("discovery")
        ctx = LLMContext(runs_db=db, settings=s, run_id=run_id, prompt_version=version, ids=deps.ids,
                         config=dict(config))
        reply = chat_call(deps.llms["discovery"].bind_tools([tool]), [*messages, *new], "discovery", ctx=ctx)
        goto = "tools" if reply.tool_calls else "triage_results"
        return Command(update={"discovery_messages": [*new, reply]}, goto=goto)

    def triage_results(state: ResearchState, config: RunnableConfig) -> dict:
        from outreach.research.triage import classify_site

        run_id, round_, checklist_key = _conf(config)
        index = result_index(deps, run_id, round_)
        city = _run_city(deps, run_id)
        for row in db.round_rows(run_id, round_, ResearchStatus.queued):
            if row["found_by_query"] == SEED_QUERY:
                continue
            title, snippet = index.get(row["lead_id"], ("", ""))
            label, _conf_ = classify_site(deps, run_id, row["lead_id"], title, snippet, dict(config))
            if label != TriageLabel.contractor_own_site:
                deps.memory_db.claim(row["lead_id"], run_id, checklist_key, city=city)
                db.set_candidate_status(run_id, row["lead_id"], ResearchStatus.rejected_triage,
                                        drop_reason=f"triage:{label}")
                deps.memory_db.finalize(row["lead_id"], run_id, ResearchStatus.rejected_triage)
                emit(ProgressEvent(kind="triage", run_id=run_id, round=round_, lead_id=row["lead_id"],
                                   status=f"triage:{label}"))
        return {}

    def next_company(state: ResearchState, config: RunnableConfig) -> Command:
        run_id, round_, checklist_key = _conf(config)
        if db.round_counted(run_id, round_) >= s.batch_size:
            return Command(update={"stop_reason": StageStopReason.filled}, goto=END)
        row = db.next_queued(run_id)
        if row is None:
            return Command(update={"stop_reason": _empty_reason(deps, run_id, round_)}, goto=END)
        if row["round"] != round_:
            db.move_to_round(run_id, row["lead_id"], round_)
        research_company(deps, run_id, round_, row["lead_id"], checklist_key,
                         child_config(dict(config), round_=round_, lead_id=row["lead_id"],
                                      run_name=f"research_company:{row['lead_id']}"))
        return Command(goto="next_company")

    g = StateGraph(ResearchState)
    g.add_node("seed_or_discover", seed_or_discover, destinations=("next_company", "triage_results", "discover"))
    g.add_node("discover", discover, destinations=("tools", "triage_results"))
    g.add_node("tools", ToolNode([tool], messages_key="discovery_messages"))
    g.add_node("triage_results", triage_results)
    g.add_node("next_company", next_company, destinations=("next_company", END))
    g.add_edge(START, "seed_or_discover")
    g.add_edge("tools", "discover")
    g.add_edge("triage_results", "next_company")
    return g.compile(checkpointer=False)


def _empty_reason(deps: Deps, run_id: str, round_: int) -> StageStopReason:
    db, s = deps.runs_db, deps.settings
    rows = db.round_rows(run_id, round_)
    if rows and all(r["found_by_query"] == SEED_QUERY for r in rows):
        return StageStopReason.seeds_done
    if len(rows) >= s.max_companies_examined_per_round:
        return StageStopReason.limit_companies
    if db.searches_in_round(run_id, round_) >= s.max_exa_searches_per_round:
        return StageStopReason.limit_searches
    return StageStopReason.queue_empty


def subgraph_recursion_limit(deps: Deps) -> int:
    s = deps.settings
    return 2 * s.max_companies_examined_per_round + 3 * s.max_exa_searches_per_round + 25


def make_run_research(deps: Deps) -> Node:
    researcher = build_researcher(deps)

    def run_research(state: dict, config: RunnableConfig) -> dict:
        run_id = state["run_id"]
        plan = deps.runs_db.plan_model(run_id)
        assert plan is not None and plan.status == "frozen"
        round_ = deps.runs_db.open_round(run_id, plan.version)
        child = child_config(dict(config), round_=round_, run_name=f"research round {round_}")
        child["configurable"].update(run_id=run_id, round=round_, checklist_key=plan.checklist.key)
        child["metadata"]["run_id"] = run_id
        child["recursion_limit"] = subgraph_recursion_limit(deps)
        final = invoke_subgraph(researcher, {"discovery_messages": []}, child)
        stop = final.get("stop_reason") or StageStopReason.queue_empty
        counted = deps.runs_db.round_counted(run_id, round_)
        deps.runs_db.close_round(run_id, round_, new_companies=counted, stop_reason=stop)
        emit(ProgressEvent(kind="round_closed", run_id=run_id, round=round_, status=str(stop),
                           counts={"new_companies": counted}))
        return {}

    return run_research

