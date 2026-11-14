"""Live smoke test (design item 57): one whole run on real APIs, kept tiny. Opt in with OUTREACH_LIVE=1.

    OUTREACH_LIVE=1 uv run pytest tests/live -s

One Exa search, research stopping at 2 counted companies (up to 8 examined), one research round, the
Claude router, real Haiku and Opus, real websites, and Hunter when HUNTER_API_KEY is set. Nothing is
mailed: the mailer only records, so this runs without Mailtrap credentials (the manual check covers real
sandbox delivery). Its databases live in a temp folder, so the real lead registry is untouched. When
LANGSMITH_API_KEY is set, it also checks that LangSmith groups the run's traces by `run_id`.
Measured cost: about $0.03 (2026-11-13).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def test_smoke_one_search_two_companies(tmp_path: Path) -> None:
    from outreach import __version__, config
    from outreach import cli as cli_mod
    from outreach.clients.exa_client import ExaClient
    from outreach.clients.hunter_client import HunterClient
    from outreach.db.memory_db import MemoryDB
    from outreach.db.runs_db import RunsDB
    from outreach.deps import Deps, SystemClock, new_ulid
    from outreach.graph.build import build_graph
    from outreach.llm import build_llms
    from outreach.research.fetcher import Fetcher
    from outreach.tls import use_os_trust_store
    from outreach.tracing import disable_tracing_if_unconfigured
    from tests.fakes import RecordingMailer
    from tests.test_cli import ScriptedScreens

    use_os_trust_store()
    base, profile, secrets = config.load(check_secrets=False)
    disable_tracing_if_unconfigured()  # after load(): the key may only be in .env
    if not (secrets.anthropic_api_key and secrets.exa_api_key):
        pytest.skip("set ANTHROPIC_API_KEY and EXA_API_KEY in .env")
    settings = base.model_copy(update={
        "runs_db_path": tmp_path / "runs.db", "memory_db_path": tmp_path / "memory.db",
        "max_exa_searches_per_round": 1, "batch_size": 2, "max_companies_examined_per_round": 8,
        "max_research_rounds": 1, "target_qualified": 2})
    clock = SystemClock()
    runs_db = RunsDB(settings.runs_db_path, now=clock.now)
    memory_db = MemoryDB(settings.memory_db_path, now=clock.now, rejected_retry_days=settings.rejected_retry_days)
    fetcher = Fetcher(settings, runs_db, clock)
    hunter = (HunterClient(secrets.hunter_api_key, settings, runs_db=runs_db, ids=new_ulid, sleep=clock.sleep)
              if secrets.hunter_api_key else None)
    mailer = RecordingMailer()
    deps = Deps(settings=settings, profile=profile, runs_db=runs_db, memory_db=memory_db,
                llms=build_llms(settings, secrets),
                exa=ExaClient(secrets.exa_api_key, settings, exclude_domains=list(profile.directory_blocklist),
                              runs_db=runs_db, ids=new_ulid, sleep=clock.sleep),
                fetcher=fetcher, hunter=hunter, mailer=mailer, clock=clock)
    screens = ScriptedScreens()
    rt = cli_mod.Runtime(deps=deps, build=build_graph, screens=screens)
    try:
        memory_db.migrate(settings.runs_db_path)
        run_id = f"smoke-{new_ulid()}"
        runs_db.create_run(run_id=run_id, app_version=__version__, router="claude",
                           prompt="find me leads in Tucson that don't use AI")
        code = cli_mod.drive(rt, run_id, {"run_id": run_id})
        print(screens.output)
        assert code == 0, screens.output
        [end] = screens.ended
        print(end)
        for query, response in runs_db.search_responses(run_id, 1):
            print(f"query: {query!r} ->", [r.get("url") for r in response.get("results", [])])
        print({r["lead_id"]: r["status"] for r in runs_db.conn.execute(
            "SELECT lead_id, status FROM run_candidates WHERE run_id = ?", (run_id,))})
        assert runs_db.get_run(run_id)["status"] == "complete"
        calls = runs_db.api_calls(run_id)
        for row in runs_db.conn.execute(  # the cost recheck in vendor_notes.md reads this table
                "SELECT vendor, call_site, COUNT(*), SUM(input_tokens), SUM(output_tokens), SUM(cached_tokens),"
                " SUM(cost_estimate) FROM api_calls WHERE run_id = ? GROUP BY vendor, call_site", (run_id,)):
            print("cost:", tuple(row))
        assert sum(1 for c in calls if c["vendor"] == "exa") == 1
        counted = runs_db.conn.execute(
            "SELECT COUNT(*) FROM run_candidates WHERE run_id = ? AND counted = 1", (run_id,)).fetchone()[0]
        assert counted <= 2
        assert end["funnel"]["sent"] == len(mailer.sent)
        assert all(m.recipient == settings.mail_test_recipient for m in mailer.sent)
        from outreach.tracing import flush

        flush()
        # Traces go to an agent address (LANGSMITH_AGENT_ID) by default, and the SDK's run query needs a
        # project, so the read-back only runs when LANGSMITH_PROJECT is set (instead of the agent vars).
        if os.environ.get("LANGSMITH_API_KEY") and os.environ.get("LANGSMITH_PROJECT"):
            from langsmith import Client

            runs = list(Client().list_runs(
                project_name=os.environ["LANGSMITH_PROJECT"],
                filter=f'and(eq(metadata_key, "run_id"), eq(metadata_value, "{run_id}"))', limit=5))
            assert runs, "no LangSmith runs carry this run_id"
        elif os.environ.get("LANGSMITH_API_KEY"):
            print(f"LangSmith: check the outreach agent in the UI for run {run_id} (no project to query)")
    finally:
        fetcher.close()
        runs_db.close()
        memory_db.close()
