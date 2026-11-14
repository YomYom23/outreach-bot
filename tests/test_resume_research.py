"""The researcher over fixture sites, and CUJ 3: a crash mid-company, then resume (design items 30, 57)."""

from collections.abc import Iterator
from pathlib import Path

import pytest

from outreach.models import COUNTED_STATUSES, ResearchStatus
from tests.sites import ResearchEnv, exa_fixture, research_env

CRASH_AT = "cf-roofing.test"  # company k: the third company researched

EXPECTED = {
    "acme-roofing.test": "candidate",
    "best-roofers-directory.test": "rejected_triage",
    "ortega-roofing.test": "candidate",
    "top10-roofers.test": "rejected_triage",
    "franchise-roof.test": "rejected_triage",
    "cf-roofing.test": "candidate",
    "mailto-only.test": "candidate",
}


@pytest.fixture
def env(tmp_path: Path) -> Iterator[ResearchEnv]:
    e = research_env(tmp_path / "a")
    yield e
    e.close()


def rows(env: ResearchEnv) -> dict[str, list[tuple]]:
    """Every runs.db row the researcher writes, minus timestamps and row ids."""
    q = env.deps.runs_db.conn.execute
    return {
        "candidates": [tuple(r) for r in q(
            "SELECT lead_id, round, status, counted, drop_reason, judge_json, us_evidence FROM run_candidates"
            " ORDER BY lead_id")],
        "evidence": [tuple(r) for r in q("SELECT evidence_id, kind, quote, page_id FROM evidence ORDER BY 1")],
        "emails": [tuple(r) for r in q("SELECT obs_id, address_norm, method, domain_relation, page_id, source_url"
                                       " FROM email_observations ORDER BY 1")],
        "phones": [tuple(r) for r in q("SELECT lead_id, e164 FROM phones_seen ORDER BY 1, 2")],
        "pages": [tuple(r) for r in q("SELECT requested_url, fetch_status, sha256, markers_json FROM pages"
                                      " ORDER BY 1")],
        "rounds": [tuple(r) for r in q("SELECT round, new_companies, stop_reason FROM research_rounds")],
        "registry": [tuple(r) for r in env.deps.memory_db.conn.execute(
            "SELECT domain_key, research_status FROM leads ORDER BY 1")],
    }


def paid_calls(env: ResearchEnv) -> dict[str, int]:
    return {"exa": len(env.exa.calls), **env.llm_calls(), "fetches": len(env.server.requests)}


# --- one round -----------------------------------------------------------------------------


def test_round_over_fixture_sites(env: ResearchEnv) -> None:
    env.run_round()
    assert env.statuses() == EXPECTED  # yelp.com is blocklisted, joe.wixsite.com is a shared host
    assert rows(env)["rounds"] == [(1, 4, "queue_empty")]
    assert paid_calls(env) == {"exa": 1, "discovery": 2, "triage": 7, "judge": 4, "fetches": 10}
    db = env.deps.runs_db
    assert {e["kind"] for e in db.evidence("R1", "acme-roofing.test")} == {"own_site", "name", "person", "location"}
    assert {(e["address_norm"], e["method"]) for e in db.email_observations("R1", "acme-roofing.test")} == {
        ("jim@acme-roofing.test", "mailto"), ("info@acme-roofing.test", "visible_text"),
        ("office@acme-roofing.test", "text_obfuscated")}
    assert db.candidate("R1", "best-roofers-directory.test")["drop_reason"] == "triage:directory"
    registry = dict(rows(env)["registry"])
    assert registry["acme-roofing.test"] == "candidate" and registry["top10-roofers.test"] == "rejected_triage"
    # Both the per-company claim and the triage-reject claim record the plan's city.
    memory = env.deps.memory_db
    assert memory.lead("acme-roofing.test")["city"] == memory.lead("top10-roofers.test")["city"] == "Tucson, AZ"


def test_tool_sees_only_new_domains_and_model_sees_objective(env: ResearchEnv) -> None:
    env.run_round()
    [(query, n)] = env.exa.calls
    assert query == "roofing contractor in Tucson, AZ" and n == env.deps.settings.exa_num_results
    tool_names = [t.name for t in env.deps.llms["discovery"].bound_tools]
    assert tool_names == ["exa_search"]
    schema = env.deps.llms["discovery"].bound_tools[0].tool_call_schema.model_json_schema()
    assert set(schema["properties"]) == {"query", "objective"} and set(schema["required"]) == {"query", "objective"}


# --- CUJ 3 -----------------------------------------------------------------------------


class Crash(RuntimeError):
    pass


def test_crash_mid_company_then_resume(env: ResearchEnv, tmp_path: Path) -> None:
    def fault(run_id: str, lead_id: str, status: ResearchStatus) -> None:
        if lead_id == CRASH_AT and status in COUNTED_STATUSES:
            raise Crash(lead_id)  # after the judge call, before the final writes

    env.deps.runs_db.fault_hook = fault
    with pytest.raises(Crash):
        env.run_round()
    before = paid_calls(env)
    assert env.statuses()[CRASH_AT] == "researching"
    assert env.deps.memory_db.lead(CRASH_AT)["research_status"] == "researching"  # registry finalized last

    env.deps.runs_db.fault_hook = None
    env.run_round()  # resume: the wrapper re-runs from its first line
    after = paid_calls(env)

    clean = research_env(tmp_path / "b")
    try:
        clean.run_round()
        # Nothing that finished before the crash is paid for again: totals equal an uninterrupted run.
        assert after == paid_calls(clean)
        # Only the company never judged before the crash (mailto-only) costs a judge call and its 2 fetches.
        assert after["judge"] - before["judge"] == 1 and after["fetches"] - before["fetches"] == 2
        assert rows(env) == rows(clean)
    finally:
        clean.close()


# --- seeds, limits, carry-over ----------------------------------------------------------------


def test_seeds_skip_discovery_and_triage(tmp_path: Path) -> None:
    env = research_env(tmp_path, seeds=["https://www.acme-roofing.test/", "yelp.com/biz/x", "ortega-roofing.test"])
    try:
        env.run_round()
        assert env.statuses() == {"acme-roofing.test": "candidate", "ortega-roofing.test": "candidate"}
        assert env.exa.calls == [] and env.llm_calls()["discovery"] == 0 and env.llm_calls()["triage"] == 0
        assert rows(env)["rounds"] == [(1, 2, "seeds_done")]
    finally:
        env.close()


def test_all_directory_round_counts_nothing(tmp_path: Path) -> None:
    env = research_env(tmp_path, exa=[exa_fixture("all_directory")])
    try:
        env.run_round()
        assert env.statuses() == {"best-roofers-directory.test": "rejected_triage",
                                  "top10-roofers.test": "rejected_triage"}
        assert rows(env)["rounds"] == [(1, 0, "queue_empty")]
        assert env.llm_calls()["judge"] == 0
    finally:
        env.close()


def test_search_limit_stops_the_tool(tmp_path: Path) -> None:
    env = research_env(tmp_path, max_exa_searches_per_round=1,
                       queries=("roofing contractor in Tucson, AZ", "roof repair company Tucson, AZ"))
    try:
        env.run_round()
        assert len(env.exa.calls) == 1
        assert env.llm_calls()["discovery"] == 1  # code stops the loop before asking the model again
        # Called anyway (a model that ignores the stop), the tool refuses without searching.
        from outreach.research.discovery import LIMIT_SEARCHES, search_and_queue

        assert search_and_queue(env.deps, "R1", 1, "another query", "o", "ck1") == LIMIT_SEARCHES
        assert len(env.exa.calls) == 1
    finally:
        env.close()


def test_company_limit_caps_the_queue(tmp_path: Path) -> None:
    env = research_env(tmp_path, max_companies_examined_per_round=3)
    try:
        env.run_round()
        assert len(env.statuses()) == 3
        assert rows(env)["rounds"][0][2] == "limit_companies"
    finally:
        env.close()


def test_batch_size_fills_and_carries_rows_into_the_next_round(tmp_path: Path) -> None:
    env = research_env(tmp_path, batch_size=1, exa=[exa_fixture("tucson_mixed"), {"results": []}],
                       queries=("roofing contractor in Tucson, AZ",))
    try:
        env.run_round()
        assert rows(env)["rounds"] == [(1, 1, "filled")]
        leftover = [d for d, s in env.statuses().items() if s == "queued"]
        assert leftover == ["ortega-roofing.test", "cf-roofing.test", "mailto-only.test"]
        env.deps.llms["discovery"].steps.extend(
            __import__("tests.sites", fromlist=["discovery_chat"]).discovery_chat("roof repair Tucson").steps)
        env.run_round()
        # The first carried-over row is researched first, and counts in round 2.
        assert env.deps.runs_db.candidate("R1", "ortega-roofing.test")["round"] == 2
        assert rows(env)["rounds"][1] == (2, 1, "filled")
    finally:
        env.close()


def test_known_domains_are_never_queued(env: ResearchEnv) -> None:
    env.deps.memory_db.block_many(["acme-roofing.test"], "older-run")
    env.run_round()
    assert "acme-roofing.test" not in env.statuses()
