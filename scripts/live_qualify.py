"""Phase 5 live check: qualify the latest live research run (scripts/live_research_round.py).

Uses the same separate databases in <runs_db dir>/live-research/. Hunter runs only if HUNTER_API_KEY is set.

    uv run python scripts/live_qualify.py

Appends a qualification table to docs/phase4_live_round.md as docs/phase5_live_qualify.md.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from outreach import config  # noqa: E402


def main() -> int:
    from outreach.tls import use_os_trust_store
    from outreach.tracing import disable_tracing_if_unconfigured

    use_os_trust_store()
    disable_tracing_if_unconfigured()
    settings, profile, secrets = config.load(check_secrets=False)
    if not secrets.anthropic_api_key:
        print("Set ANTHROPIC_API_KEY in .env first.", file=sys.stderr)
        return 3

    from outreach.clients.hunter_client import HunterClient
    from outreach.db.memory_db import MemoryDB
    from outreach.db.runs_db import RunsDB
    from outreach.deps import Deps, SystemClock, new_ulid
    from outreach.llm import build_llms
    from outreach.qualify.subgraph import make_run_qualify

    root = Path(settings.runs_db_path).parent / "live-research"
    clock = SystemClock()
    runs_db = RunsDB(root / "runs.db", now=clock.now)
    memory_db = MemoryDB(root / "memory.db", now=clock.now, rejected_retry_days=settings.rejected_retry_days)
    row = runs_db.conn.execute("SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    if row is None:
        print("No live research run yet: run scripts/live_research_round.py first.", file=sys.stderr)
        return 2
    run_id = row[0]
    hunter = (HunterClient(secrets.hunter_api_key, settings, runs_db=runs_db, ids=new_ulid, sleep=clock.sleep)
              if secrets.hunter_api_key else None)
    deps = Deps(settings=settings, profile=profile, runs_db=runs_db, memory_db=memory_db,
                llms=build_llms(settings, secrets), hunter=hunter, clock=clock)
    plan = runs_db.plan_model(run_id)
    assert plan is not None
    print(f"Qualifying {run_id} (N = {plan.checklist.threshold}; Hunter {'on' if hunter else 'off: no key'})...")
    make_run_qualify(deps)({"run_id": run_id}, {})

    items = {i.id: i.text for i in plan.checklist.items}
    lines = [f"# Phase 5 live qualification of `{run_id}`", "",
             f"Checklist (N = {plan.checklist.threshold}): " + "; ".join(f"`{k}` {v}" for k, v in items.items()),
             f"Hunter: {'on' if hunter else 'off (no HUNTER_API_KEY)'}.", "",
             "| Domain | Points | Verdicts | Qualified | Email (type, source) | Greeting | Drop |",
             "|---|---|---|---|---|---|---|"]
    for q in runs_db.conn.execute("SELECT * FROM qualifications WHERE run_id = ? ORDER BY points DESC, lead_id",
                                  (run_id,)):
        verdicts = ", ".join(f"{v['item_id']}={v['value']}" for v in json.loads(q["verdicts_json"]))
        email = f"{q['email']} ({q['email_type']}, {q['email_source_kind']})" if q["email"] else ""
        lines.append(f"| {q['lead_id']} | {q['points']} | {verdicts} | {'yes' if q['qualified'] else 'no'} | "
                     f"{email} | {q['greeting_name'] or 'Hi there'} | {q['drop_reason'] or ''} |")
    calls = runs_db.api_calls(run_id, call_site="checklist")
    lines += ["", f"Checklist calls: {len(calls)}, about ${sum(c['cost_estimate'] or 0 for c in calls):.4f}."]
    (REPO / "docs" / "phase5_live_qualify.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    runs_db.close()
    memory_db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
