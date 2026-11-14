"""Phase 4 test support: the fixture websites behind httpx.MockTransport, and scripted Haiku replies.

`tests/fixtures/sites/<domain>/<page>.html` is served at `https://<domain>/<page>` ("index" is "/").
Every `*.test` host resolves to a public address, so the real Fetcher runs its full guard path offline.
"""

from __future__ import annotations

import json
import re
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from outreach import __version__
from outreach.db.memory_db import MemoryDB
from outreach.db.runs_db import RunsDB
from outreach.deps import Deps
from outreach.models import JudgeResult, Location, Person, TriageLabel, TriageOut
from outreach.research.fetcher import Fetcher
from tests.fakes import FakeChat, FakeClock, FakeExa, SeqIds, Step

FIXTURES = Path(__file__).parent / "fixtures"
SITES = FIXTURES / "sites"
PUBLIC_IP = "93.184.216.34"
SOFT_404_DOMAINS = {"soft404-roofing.test"}


def site_page(host: str, path: str) -> str | None:
    host = host.removeprefix("www.")
    name = path.strip("/") or "index"
    file = SITES / host / f"{name}.html"
    if file.exists():
        return file.read_text(encoding="utf-8")
    if host in SOFT_404_DOMAINS:
        return (SITES / host / "index.html").read_text(encoding="utf-8")
    return None


class SiteServer:
    """The fixture sites as an httpx transport, with a request log and per-URL overrides."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response]] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        if url in self.overrides:
            return self.overrides[url](request)
        html = site_page(request.url.host, request.url.path)
        if html is None:
            return httpx.Response(404, text="not found", headers={"content-type": "text/html"})
        return httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def hits(self) -> Counter:
        return Counter(self.requests)


def public_resolver(host: str) -> list[str]:
    return [PUBLIC_IP]


def make_fetcher(deps: Any, server: SiteServer | None = None,
                 resolver: Callable[[str], list[str]] = public_resolver) -> Fetcher:
    server = server or SiteServer()
    return Fetcher(deps.settings, deps.runs_db, deps.clock, resolver=resolver, transport=server.transport)


def exa_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "exa" / f"{name}.json").read_text(encoding="utf-8"))


# --- scripted Haiku -----------------------------------------------------------------

TRIAGE_BY_DOMAIN = {
    "best-roofers-directory.test": TriageLabel.directory,
    "top10-roofers.test": TriageLabel.listicle,
    "franchise-roof.test": TriageLabel.franchise_hub,
}


def triage_chat() -> FakeChat:
    def respond(messages: list[Any]) -> TriageOut:
        domain = re.search(r'<result domain="([^"]+)"', messages[-1].content).group(1)  # type: ignore[union-attr]
        label = TRIAGE_BY_DOMAIN.get(domain, TriageLabel.contractor_own_site)
        return TriageOut(label=label, confidence=0.9, reason="scripted")

    return FakeChat(model="claude-haiku-5-5", call_site="triage", responder=respond)


def _page_ids(text: str) -> dict[str, int]:
    return {m.group(1): int(m.group(2)) for m in re.finditer(r'url="https://[^/"]+(/[^"]*)" page_id="(\d+)"', text)}


def judge_reply(text: str) -> JudgeResult:
    """What a well-behaved judge would say for each fixture site, with quotes copied from the pages."""
    domain = re.search(r'url="https://([^/"]+)/', text).group(1)  # type: ignore[union-attr]
    ids = _page_ids(text)
    if domain == "acme-roofing.test":
        about = ids.get("/about-us")
        return JudgeResult(
            is_single_roofer_own_site=True, own_site_quote="Tucson's family-owned roofer since 1998.",
            company_name="Acme Roofing", name_quote="Acme Roofing",
            people=[Person(name="Jim Carter", role="owner", quote="Jim Carter, owner of Acme Roofing", page_id=about),
                    Person(name="Dana Carter", role="office", quote="Dana Carter runs the office", page_id=about)],
            locations=[Location(text="Tucson, AZ", quote="Tucson, AZ 85705", page_id=ids.get("/contact"))])
    if domain == "ortega-roofing.test":
        story = ids.get("/our-story")
        return JudgeResult(
            is_single_roofer_own_site=True, own_site_quote="co-owners of Ortega Roofing",
            company_name="Ortega Roofing", name_quote="Ortega Roofing",
            people=[Person(name="Maria Ortega", role="co-owner", quote="Maria Ortega and Luis Ortega are the co-owners",
                           page_id=story),
                    Person(name="Luis Ortega", role="co-owner", quote="Maria Ortega and Luis Ortega are the co-owners",
                           page_id=story)])
    if domain == "injection-roofing.test":
        # A judge that obeyed the injected text and embellished it: these quotes are not on any page.
        return JudgeResult(
            is_single_roofer_own_site=True, own_site_quote="Bob Smith has owned Injection Roofing since 1950",
            company_name="Injection Roofing", name_quote="Injection Roofing",
            people=[Person(name="Bob Smith", role="owner", quote="Bob Smith has owned Injection Roofing since 1950")])
    name = domain.split(".")[0].replace("-", " ").title()
    return JudgeResult(is_single_roofer_own_site=True, own_site_quote="We install, repair and replace asphalt shingle",
                       company_name=name, name_quote=None)


def judge_chat() -> FakeChat:
    return FakeChat(model="claude-haiku-5-5", call_site="judge", responder=lambda m: judge_reply(m[-1].content))


def tool_call(query: str, objective: str = "Roofers' own sites first; no directories.", n: int = 1) -> dict:
    return {"name": "exa_search", "args": {"query": query, "objective": objective}, "id": f"call_{n}",
            "type": "tool_call"}


def discovery_chat(*queries: str) -> FakeChat:
    """One tool call per query, then a final reply with no tool call."""
    steps = [Step("tool_calls", [tool_call(q, n=i)]) for i, q in enumerate(queries, 1)]
    return FakeChat([*steps, Step("ok", "Enough companies found.")], model="claude-haiku-5-5", call_site="discovery")


def research_llms(*queries: str) -> dict[str, FakeChat]:
    return {"discovery": discovery_chat(*(queries or ("roofing contractor in Tucson, AZ",))),
            "triage": triage_chat(), "judge": judge_chat()}


# --- a whole research environment ------------------------------------------------------


@dataclass
class ResearchEnv:
    deps: Deps
    server: SiteServer
    run_id: str = "R1"

    def run_round(self) -> dict:
        from outreach.research.subgraph import make_run_research

        return make_run_research(self.deps)({"run_id": self.run_id}, {})

    @property
    def exa(self) -> FakeExa:
        return self.deps.exa  # type: ignore[no-any-return]

    def statuses(self) -> dict[str, str]:
        return {r["lead_id"]: r["status"] for r in self.deps.runs_db.conn.execute(
            "SELECT lead_id, status FROM run_candidates WHERE run_id = ? ORDER BY queue_pos", (self.run_id,))}

    def llm_calls(self) -> dict[str, int]:
        return {site: chat.call_count for site, chat in self.deps.llms.items()}

    def close(self) -> None:
        self.deps.fetcher.close()
        self.deps.runs_db.close()
        self.deps.memory_db.close()


def research_env(root: Path, *, exa: list[Any] | None = None, seeds: list[str] | None = None,
                 queries: tuple[str, ...] = (), run: bool = True, **settings: Any) -> ResearchEnv:
    """A run with a frozen plan (Tucson, AZ), the fixture sites, scripted Haiku and a scripted Exa.
    With `run=False` no run is created: the CLI creates it (test_end_to_end_fake)."""
    from tests.conftest import load_profile, make_settings

    root.mkdir(parents=True, exist_ok=True)
    s = make_settings(root, **settings)
    clock = FakeClock()
    runs_db = RunsDB(s.runs_db_path, now=clock.now)
    memory_db = MemoryDB(s.memory_db_path, now=clock.now, rejected_retry_days=s.rejected_retry_days)
    memory_db.migrate(None)
    server = SiteServer()
    deps = Deps(settings=s, profile=load_profile(), runs_db=runs_db, memory_db=memory_db,
                llms=research_llms(*queries),
                exa=FakeExa(deque(exa if exa is not None else [exa_fixture("tucson_mixed")])),
                clock=clock, ids=SeqIds())
    deps.fetcher = make_fetcher(deps, server)
    if not run:
        return ResearchEnv(deps, server)
    runs_db.create_run(run_id="R1", app_version=__version__, router="code", prompt="p", seeds=seeds)
    runs_db.write_plan("R1", version=1, status="frozen", city="Tucson, AZ", target_qualified=4,
                       checklist={"items": [{"id": "i1", "text": "t", "check": "llm", "code_check": None}],
                                  "threshold": 1}, sender={}, checklist_key="ck1")
    return ResearchEnv(deps, server)
