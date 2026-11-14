"""Phase 5 test support: leads written straight into runs.db, and a scripted checklist judge."""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from outreach.models import ChecklistOut, Person, ResearchStatus, Verdict, VerdictValue
from outreach.qualify.subgraph import make_run_qualify
from tests.fakes import FakeChat, FakeHunter
from tests.sites import FIXTURES, ResearchEnv, research_env

PAGE_TEXT = ("Acme Roofing is a family-owned roofer. We repair and replace roofs for homeowners across Tucson. "
             "Call for a free estimate.")
DEFAULT_ITEMS = [
    {"id": "no_ai", "text": "No AI chat or automation tool is visible", "check": "code", "code_check": "no_ai_visible"},
    {"id": "family", "text": "Owner-operated or family-owned business", "check": "llm", "code_check": None},
    {"id": "residential", "text": "Does residential roofing work", "check": "llm", "code_check": None},
]


def hunter_fixture(name: str) -> dict:
    return json.loads((FIXTURES / "hunter" / f"{name}.json").read_text(encoding="utf-8"))


class ChecklistJudge:
    """Replies per lead with scripted verdicts; by default every llm item is met, quoting the page."""

    def __init__(self) -> None:
        self.by_domain: dict[str, dict[str, tuple[str, str | None]]] = {}
        self.chat = FakeChat(model="claude-haiku-5-5", call_site="checklist", responder=self.reply)

    def reply(self, messages: list[Any]) -> ChecklistOut:
        text = messages[-1].content
        domain = re.search(r'url="https://([^/"]+)/', text).group(1)  # type: ignore[union-attr]
        page_id = int(re.search(r'page_id="(\d+)"', text).group(1))  # type: ignore[union-attr]
        ids = re.findall(r"^- (\w+): ", text, re.MULTILINE)
        script = self.by_domain.get(domain, {})
        out = []
        for item_id in ids:
            value, quote = script.get(item_id, ("met", "family-owned roofer" if item_id == "family" else "homeowners"))
            out.append(Verdict(item_id=item_id, value=VerdictValue(value), quote=quote, page_id=page_id))
        return ChecklistOut(verdicts=out)


class QualifyEnv:
    def __init__(self, env: ResearchEnv, judge: ChecklistJudge) -> None:
        self.env, self.judge = env, judge
        self.deps = env.deps

    @property
    def hunter(self) -> FakeHunter:
        return self.deps.hunter  # type: ignore[no-any-return]

    def add_lead(self, domain: str, *, emails: list[tuple[str, str]] = (), ok_pages: int = 2,  # type: ignore[assignment]
                 markers: list[str] = (), people: list[Person] = (), company: str | None = None,  # type: ignore[assignment]
                 text: str = PAGE_TEXT, status: ResearchStatus = ResearchStatus.candidate) -> None:
        """A researched lead: `ok_pages` pages of `text`, on-site `emails` as (address, relation)."""
        db = self.deps.runs_db
        db.queue_candidates("R1", 1, [domain], found_by_query="test")
        for n in range(ok_pages):
            path = "/" if n == 0 else f"/p{n}"
            page_id = db.page_put(run_id="R1", lead_id=domain, requested_url=f"https://{domain}{path}",
                                  fetch_status="ok", clean_text=text)
            db.set_page_markers(page_id, list(markers) if n == 0 else [])
        for address, relation in emails:
            db.add_email_observation(obs_id=f"R1:{domain}:{address}:visible_text", run_id="R1", lead_id=domain,
                                     address_norm=address, address_as_seen=address, page_id=None,
                                     source_url=f"https://{domain}/contact", method="visible_text",
                                     domain_relation=relation)
        judge = {"judge": {"is_single_roofer_own_site": True, "company_name": company,
                           "people": [p.model_dump() for p in people], "locations": []}}
        db.set_candidate_status("R1", domain, ResearchStatus.researching)
        db.set_candidate_status("R1", domain, status, judge_json=json.dumps(judge), us_evidence="address:AZ 85701")
        self.deps.memory_db.claim(domain, "R1", "ck1")

    def set_checklist(self, threshold: int, items: list[dict] | None = None, version: int = 2) -> None:
        self.deps.runs_db.write_plan("R1", version=version, status="frozen", city="Tucson, AZ", target_qualified=4,
                                     checklist={"items": items or DEFAULT_ITEMS, "threshold": threshold},
                                     sender={}, checklist_key="ck1")

    def run(self) -> None:
        make_run_qualify(self.deps)({"run_id": "R1"}, {})

    def q(self, domain: str) -> dict:
        row = self.deps.runs_db.qualification("R1", domain)
        assert row is not None, f"no qualifications row for {domain}"
        return dict(row)

    def checklist_calls(self) -> int:
        return self.judge.chat.call_count


@contextmanager
def qualify_env(root: Path, **settings: Any) -> Iterator[QualifyEnv]:
    env = research_env(root, **settings)
    judge = ChecklistJudge()
    env.deps.llms["checklist"] = judge.chat
    env.deps.hunter = FakeHunter(runs_db=env.deps.runs_db)
    env.deps.runs_db.open_round("R1", 1)
    qe = QualifyEnv(env, judge)
    qe.set_checklist(2)
    try:
        yield qe
    finally:
        env.close()


HunterFactory = Callable[[str], dict]
