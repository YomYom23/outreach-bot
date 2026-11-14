"""Phase 3b probe (design item 59): is Exa a good enough source of roofers' own sites?

For each query wording x city it runs one Exa search (25 results), labels each result with the Haiku
triage prompt, and checks the home and /contact pages of each own-site result for an email address.
Then it runs Hunter Domain Search on up to 10 own-site domains that showed no email ("form-only").

Go/no-go: if the average number of own sites per search is below 5 of 25, it's a no-go, and v1 runs
only with --seeds.

    uv run python scripts/probe_exa_hunter.py --dry-run     # the plan and call counts, no network
    uv run python scripts/probe_exa_hunter.py               # live: needs ANTHROPIC_API_KEY, EXA_API_KEY

Writes docs/probe_3b_results.md and docs/probe_3b_results.json. Calls are recorded in probe.db, next to
runs.db, so a second run reads triage results from llm_cache instead of paying again.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from outreach import config  # noqa: E402
from outreach.models import TriageLabel, TriageOut  # noqa: E402

GO_THRESHOLD = 5
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def domain_key(url: str) -> str:
    import tldextract

    ext = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None, include_psl_private_domains=True)(url.lower())
    return ext.top_domain_under_public_suffix or ext.fqdn


def page_emails(html: str, domain: str, profile: Any) -> set[str]:
    found = set(EMAIL_RE.findall(html))
    out = set()
    for addr in found:
        addr = addr.lower().strip(".")
        tld = addr.rsplit(".", 1)[-1]
        host = addr.split("@", 1)[1]
        if tld in profile.file_extension_tlds or addr in profile.placeholder_addresses:
            continue
        if any(host == v or host.endswith("." + v) for v in profile.vendor_denylist):
            continue
        out.add(addr)
    if "data-cfemail" in html or "/cdn-cgi/l/email-protection" in html:
        out.add(f"(cloudflare-protected)@{domain}")
    return out


def fetch_emails(http: Any, domain: str, settings: Any, profile: Any) -> tuple[set[str], list[str]]:
    emails: set[str] = set()
    notes = []
    for path in ("/", "/contact", "/contact-us"):
        try:
            r = http.get(f"https://{domain}{path}")
        except Exception as e:  # probe only: note it and move on
            notes.append(f"{path}: {type(e).__name__}")
            continue
        if r.status_code == 200 and "html" in r.headers.get("content-type", ""):
            emails |= page_emails(r.text[: settings.max_page_bytes], domain, profile)
        else:
            notes.append(f"{path}: HTTP {r.status_code}")
        time.sleep(settings.per_host_delay_s)
        if emails:
            break
    return emails, notes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--queries", type=int, default=5, help="query wordings (3-5)")
    ap.add_argument("--cities", type=int, default=3)
    ap.add_argument("--num-results", type=int, default=25)
    ap.add_argument("--hunter", type=int, default=10, help="form-only domains to try with Hunter (0 skips)")
    ap.add_argument("--no-category", action="store_true",
                    help="search without category=company, to compare how many own sites each finds")
    args = ap.parse_args()

    settings, profile, secrets = config.load(check_secrets=False)
    templates = [*profile.query_templates, *profile.retry_query_templates][: args.queries]
    cities = list(profile.fallback_cities[: args.cities])
    plan = [(t.format(city=c), c) for c in cities for t in templates]
    print(f"{len(plan)} Exa searches x {args.num_results} results; up to {len(plan) * args.num_results} Haiku "
          f"triage calls (cached by domain); up to {args.hunter} Hunter searches.")
    for q, _ in plan:
        print(f"  - {q}")
    if args.dry_run:
        return 0
    missing = [n for n, v in (("ANTHROPIC_API_KEY", secrets.anthropic_api_key),
                              ("EXA_API_KEY", secrets.exa_api_key)) if not v]
    if missing:
        print(f"Set {', '.join(missing)} in .env first.", file=sys.stderr)
        return 3

    import httpx
    from langchain_core.messages import HumanMessage

    from outreach.clients.exa_client import ExaClient
    from outreach.clients.hunter_client import HunterClient
    from outreach.db.runs_db import RunsDB
    from outreach.deps import new_ulid
    from outreach.llm import LLMContext, build_llms, load_prompt, structured_call
    from outreach.tls import use_os_trust_store
    from outreach.tracing import disable_tracing_if_unconfigured, flush

    use_os_trust_store()
    disable_tracing_if_unconfigured()
    db = RunsDB(Path(settings.runs_db_path).parent / "probe.db")
    run_id = f"probe-{new_ulid()}"
    exa = ExaClient(secrets.exa_api_key, settings, exclude_domains=list(profile.directory_blocklist), runs_db=db,
                    ids=new_ulid, category=None if args.no_category else "company")
    triage_llm = build_llms(settings, secrets)["triage"]
    triage_prompt, triage_version = load_prompt("triage")
    ctx = LLMContext(runs_db=db, settings=settings, run_id=run_id, prompt_version=triage_version, ids=new_ulid)
    http = httpx.Client(follow_redirects=True, headers={"User-Agent": settings.user_agent},
                        timeout=httpx.Timeout(settings.read_timeout_s, connect=settings.connect_timeout_s))

    labels: dict[str, str] = {}
    emails_by_domain: dict[str, set[str]] = {}
    rows = []
    for query, city in plan:
        objective = (f"Find the websites of individual local {profile.industry} companies serving {city}. "
                     "Rank each company's own website first. Exclude directories, review sites, lead-generation "
                     "sites, ranking articles, manufacturers and suppliers, and national franchise brand pages.")
        resp = exa.search(query, args.num_results, objective=objective, run_id=run_id, call_site="probe")
        own, own_email = 0, 0
        for r in resp["results"]:
            d = domain_key(r["url"])
            if d not in labels:
                msg = triage_prompt.format(industry=profile.industry, domain=d, title=r["title"][:200],
                                           snippet=" ".join(r["highlights"])[: settings.snippet_chars])
                out = structured_call(triage_llm, TriageOut, [HumanMessage(content=msg)], "triage", ctx=ctx)
                labels[d] = out.label.value if out else "other"
            if labels[d] != TriageLabel.contractor_own_site:
                continue
            own += 1
            if d not in emails_by_domain:
                emails_by_domain[d], _ = fetch_emails(http, d, settings, profile)
            own_email += bool(emails_by_domain[d])
        rows.append({"query": query, "city": city, "results": len(resp["results"]), "own_sites": own,
                     "own_sites_with_email": own_email, "cost": resp["cost_dollars"]["total"]})
        print(f"{own:2}/{len(resp['results'])} own sites, {own_email} with email: {query}")

    hunter_rows = []
    form_only = [d for d, e in emails_by_domain.items() if not e][: args.hunter]
    if form_only and secrets.hunter_api_key:
        hunter = HunterClient(secrets.hunter_api_key, settings, runs_db=db, ids=new_ulid)
        print(f"Hunter credits remaining before: {hunter.credits_remaining()}")
        for d in form_only:
            body = hunter.domain_search(d, run_id=run_id)
            emails = (body.get("data") or {}).get("emails") or []
            sourced = [e for e in emails if any(s.get("uri") for s in e.get("sources") or [])]
            hunter_rows.append({
                "domain": d, "emails": len(emails), "sourced": len(sourced),
                "executive": sum(e.get("seniority") == "executive" for e in sourced),
                "generic": sum(e.get("type") == "generic" for e in sourced)})
            print(f"hunter {d}: {len(sourced)} sourced of {len(emails)}")
    elif form_only:
        print("HUNTER_API_KEY not set: Hunter section skipped.")

    calls = [dict(c) for c in db.api_calls(run_id)]
    triage_calls = [c for c in calls if c["call_site"] == "triage"]
    exa_costs = [c["cost_estimate"] for c in calls if c["vendor"] == "exa" and c["cost_estimate"] is not None]
    avg_own = mean(r["own_sites"] for r in rows) if rows else 0.0
    summary = {
        "date": datetime.now(UTC).date().isoformat(), "run_id": run_id,
        "avg_own_sites_per_search": round(avg_own, 2),
        "avg_own_sites_with_email": round(mean(r["own_sites_with_email"] for r in rows), 2) if rows else 0.0,
        "decision": "GO" if avg_own >= GO_THRESHOLD else "NO-GO (v1 runs only with --seeds)",
        "category": None if args.no_category else "company",
        "exa_cost_per_call": round(mean(exa_costs), 5) if exa_costs else None,
        "triage_tokens_avg": {
            "input": round(mean(c["input_tokens"] or 0 for c in triage_calls)) if triage_calls else None,
            "output": round(mean(c["output_tokens"] or 0 for c in triage_calls)) if triage_calls else None,
            "max_output": max((c["output_tokens"] or 0 for c in triage_calls), default=None)},
        "triage_stop_reasons": sorted({c["stop_reason"] for c in triage_calls if c["stop_reason"]}),
        "label_counts": {lbl: list(labels.values()).count(lbl) for lbl in sorted(set(labels.values()))},
        "searches": rows, "hunter": hunter_rows,
    }
    out_json = REPO / "docs" / "probe_3b_results.json"
    out_json.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    lines = [f"# Phase 3b probe results ({summary['date']})", "",
             f"**Decision: {summary['decision']}** (average {avg_own:.1f} own sites per search; "
             f"threshold {GO_THRESHOLD} of {args.num_results}; category: {summary['category'] or 'none'}).", "",
             "| Query | Own sites | With email | Exa cost |", "|---|---|---|---|"]
    lines += [f"| {r['query']} | {r['own_sites']}/{r['results']} | {r['own_sites_with_email']} | {r['cost']} |"
              for r in rows]
    lines += ["", f"- Exa cost per call: {summary['exa_cost_per_call']}",
              f"- Triage tokens (avg in/out, max out): {summary['triage_tokens_avg']}",
              f"- Triage labels: {summary['label_counts']}"]
    if hunter_rows:
        lines += ["", "| Hunter domain | Emails | With source | Executive | Generic |", "|---|---|---|---|---|"]
        lines += [f"| {h['domain']} | {h['emails']} | {h['sourced']} | {h['executive']} | {h['generic']} |"
                  for h in hunter_rows]
    (REPO / "docs" / "probe_3b_results.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{summary['decision']}. Report: docs/probe_3b_results.md")
    flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
