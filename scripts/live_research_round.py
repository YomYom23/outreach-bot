"""Phase 4 live check: one real research round (Exa + Haiku + real websites), outside the parent graph.

Uses its own databases in <runs_db dir>/live-research/, so the lead registry of real runs is untouched.

    uv run python scripts/live_research_round.py                 # Tucson, AZ
    uv run python scripts/live_research_round.py --city "Mesa, AZ"

Writes docs/phase4_live_round.md.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from outreach import __version__, config  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--city", default="Tucson, AZ")
    args = ap.parse_args()

    from outreach.tls import use_os_trust_store
    from outreach.tracing import disable_tracing_if_unconfigured

    use_os_trust_store()
    disable_tracing_if_unconfigured()
    settings, profile, secrets = config.load(check_secrets=False)
    missing = [n for n, v in (("ANTHROPIC_API_KEY", secrets.anthropic_api_key),
                              ("EXA_API_KEY", secrets.exa_api_key)) if not v]
    if missing:
        print(f"Set {', '.join(missing)} in .env first.", file=sys.stderr)
        return 3

    from outreach.clients.exa_client import ExaClient
    from outreach.db.memory_db import MemoryDB
    from outreach.db.runs_db import RunsDB
    from outreach.deps import Deps, SystemClock, new_ulid
    from outreach.llm import build_llms
    from outreach.research.fetcher import Fetcher
    from outreach.research.subgraph import make_run_research

    root = Path(settings.runs_db_path).parent / "live-research"
    root.mkdir(parents=True, exist_ok=True)
    clock = SystemClock()
    runs_db = RunsDB(root / "runs.db", now=clock.now)
    memory_db = MemoryDB(root / "memory.db", now=clock.now, rejected_retry_days=settings.rejected_retry_days)
    memory_db.migrate(None)
    exa = ExaClient(secrets.exa_api_key, settings, exclude_domains=list(profile.directory_blocklist),
                    runs_db=runs_db, ids=new_ulid, sleep=clock.sleep)
    fetcher = Fetcher(settings, runs_db, clock)
    deps = Deps(settings=settings, profile=profile, runs_db=runs_db, memory_db=memory_db,
                llms=build_llms(settings, secrets), exa=exa, fetcher=fetcher, clock=clock)

    run_id = f"live-{new_ulid()}"
    checklist = profile.default_checklist
    runs_db.create_run(run_id=run_id, app_version=__version__, router="code", prompt=f"roofers in {args.city}")
    runs_db.write_plan(run_id, version=1, status="frozen", city=args.city, target_qualified=settings.target_qualified,
                       checklist={"items": [{"id": f"i{n}", **i.model_dump()} for n, i in enumerate(checklist.items)],
                                  "threshold": checklist.threshold},
                       sender={}, checklist_key=f"live-{run_id}")
    started = time.monotonic()
    print(f"{run_id}: researching {args.city} (batch_size {settings.batch_size}, "
          f"up to {settings.max_exa_searches_per_round} searches)...")
    make_run_research(deps)({"run_id": run_id}, {})
    elapsed = time.monotonic() - started

    q = runs_db.conn.execute
    cands = [dict(r) for r in q("SELECT lead_id, status, drop_reason, us_evidence, judge_json FROM run_candidates"
                                " WHERE run_id = ? ORDER BY queue_pos", (run_id,))]
    rnd = dict(q("SELECT * FROM research_rounds WHERE run_id = ?", (run_id,)).fetchone())
    calls = [dict(r) for r in runs_db.api_calls(run_id)]
    by_site: Counter = Counter(c["call_site"] for c in calls)
    cost = sum(c["cost_estimate"] or 0 for c in calls)
    pages = Counter(r[0] for r in q("SELECT fetch_status FROM pages WHERE run_id = ?", (run_id,)))
    queries = runs_db.queries_tried(run_id)

    lines = [f"# Phase 4 live research round ({datetime.now(UTC).date().isoformat()})", "",
             f"City: {args.city}. Run `{run_id}`. {elapsed:.0f} s. Round stop: `{rnd['stop_reason']}`, "
             f"{rnd['new_companies']} counted companies. Estimated cost: ${cost:.3f}.", "",
             "Queries the discovery model chose:", *[f"- {x}" for x in queries], "",
             f"Calls: {dict(by_site)}. Pages: {dict(pages)}.", "",
             "| Domain | Status | Emails (relation) | US evidence | Company / people |", "|---|---|---|---|---|"]
    for c in cands:
        emails = ", ".join(f"{r['address_norm']} ({r['domain_relation']}, {r['method']})"
                           for r in runs_db.email_observations(run_id, c["lead_id"]))
        judge = (json.loads(c["judge_json"]) or {}).get("judge") if c["judge_json"] else None
        who = ""
        if judge:
            people = "; ".join(f"{p['name']} ({p.get('role') or '?'})" for p in judge.get("people", []))
            who = f"{judge.get('company_name') or ''}" + (f": {people}" if people else "")
        lines.append(f"| {c['lead_id']} | {c['status']}{' (' + c['drop_reason'] + ')' if c['drop_reason'] else ''}"
                     f" | {emails} | {c['us_evidence'] or ''} | {who} |")
    out = REPO / "docs" / "phase4_live_round.md"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    fetcher.close()
    runs_db.close()
    memory_db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
