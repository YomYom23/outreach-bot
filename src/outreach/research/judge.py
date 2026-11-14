"""The per-company judge (design item 37) and the status decision that follows it.

One Haiku call reads the clean text of the company's `ok` pages and returns a `JudgeResult`. The model only
proposes: code keeps a claim only when its quote is found in the stored page text (`quote_found`), and code,
not the model, decides US evidence and the research status.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from outreach.llm import LLMContext, load_prompt, structured_call
from outreach.models import DomainRelation, JudgeResult, ResearchStatus
from outreach.research.extract import quote_found

if TYPE_CHECKING:
    from outreach.deps import Deps
    from outreach.research.extract import FoundEmail
    from outreach.research.fetcher import PageRecord

ON_SITE = (DomainRelation.same_domain, DomainRelation.freemail)


def format_pages(pages: Sequence[PageRecord], chars: int) -> str:
    """Each page inside delimiters the prompt names, so page text can't pose as instructions."""
    return "\n\n".join(
        f'<page_content url="{p.requested_url}" page_id="{p.page_id}">\n{p.clean_text[:chars]}\n</page_content>'
        for p in pages)


def judge_company(deps: Deps, run_id: str, pages: Sequence[PageRecord],
                  config: dict | None = None) -> JudgeResult | None:
    if not pages:
        return None
    prompt, version = load_prompt("company_judge")
    msg = prompt.format(industry=deps.profile.industry,
                        pages=format_pages(pages, deps.settings.page_text_chars))
    ctx = LLMContext(runs_db=deps.runs_db, settings=deps.settings, run_id=run_id, prompt_version=version,
                     ids=deps.ids, config=config)
    return structured_call(deps.llms["judge"], JudgeResult, [HumanMessage(content=msg)], "judge", ctx=ctx)


def verify_quotes(result: JudgeResult, pages: Sequence[PageRecord], chars: int) -> JudgeResult:
    """Drops every claim whose quote isn't in the text the model was shown."""
    texts = [p.clean_text[:chars] for p in pages]
    own_ok = quote_found(result.own_site_quote, texts)
    name_ok = quote_found(result.name_quote, texts)
    return JudgeResult(
        is_single_roofer_own_site=result.is_single_roofer_own_site and own_ok,
        own_site_quote=result.own_site_quote if own_ok else None,
        company_name=result.company_name if name_ok else None,
        name_quote=result.name_quote if name_ok else None,
        people=[p for p in result.people if quote_found(p.quote, texts)],
        locations=[loc for loc in result.locations if quote_found(loc.quote, texts)],
    )


def decide_status(verified: JudgeResult | None, us_evidence: str | None,
                  emails: Sequence[FoundEmail]) -> ResearchStatus:
    if verified is None or not verified.is_single_roofer_own_site or not verified.own_site_quote:
        return ResearchStatus.rejected_not_roofer
    if us_evidence is None:
        return ResearchStatus.rejected_non_us
    if any(e.domain_relation in ON_SITE for e in emails):
        return ResearchStatus.candidate
    return ResearchStatus.parked_no_email
