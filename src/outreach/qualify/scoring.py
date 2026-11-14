"""score_lead (design item 40; Decisions: Checklist scoring; Rules: no_ai_visible, Quote check).

`code` items run a registered function over runs.db rows. `llm` items share one Haiku call per lead, over
the same page text the researcher stored; its verdicts are cached, so re-qualifying after `lower_threshold`
costs nothing. A verdict whose quote isn't on the page becomes "unknown". points = number of "met".
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from outreach.llm import LLMContext, load_prompt, structured_call
from outreach.models import ChecklistItem, ChecklistOut, FetchStatus, Verdict, VerdictValue
from outreach.research.extract import quote_found

if TYPE_CHECKING:
    from sqlite3 import Row

    from outreach.deps import Deps
    from outreach.models import RunPlan

CodeCheck = Callable[["Deps", str, str, ChecklistItem], Verdict]


def ok_pages(deps: Deps, run_id: str, lead_id: str) -> list[Row]:
    return [p for p in deps.runs_db.pages_for_lead(run_id, lead_id) if p["fetch_status"] == FetchStatus.ok]


def no_ai_visible(deps: Deps, run_id: str, lead_id: str, item: ChecklistItem) -> Verdict:
    """unknown below `no_ai_min_ok_pages` ok pages; otherwise not_met if any AI-tool marker appears; else met."""
    pages = ok_pages(deps, run_id, lead_id)
    if len(pages) < deps.settings.no_ai_min_ok_pages:
        return Verdict(item_id=item.id, value=VerdictValue.unknown)
    for p in pages:
        markers = json.loads(p["markers_json"] or "[]")
        if markers:
            return Verdict(item_id=item.id, value=VerdictValue.not_met, quote=f"marker:{markers[0]}",
                           page_id=p["page_id"])
    return Verdict(item_id=item.id, value=VerdictValue.met)


CODE_CHECKS: dict[str, CodeCheck] = {"no_ai_visible": no_ai_visible}


def format_items(items: Sequence[ChecklistItem]) -> str:
    return "\n".join(f"- {i.id}: {i.text}" for i in items)


def format_pages(pages: Sequence[Row], chars: int) -> str:
    return "\n\n".join(
        f'<page_content url="{p["requested_url"]}" page_id="{p["page_id"]}">\n{(p["clean_text"] or "")[:chars]}\n'
        "</page_content>" for p in pages)


def llm_verdicts(deps: Deps, run_id: str, lead_id: str, items: Sequence[ChecklistItem],
                 config: dict | None = None) -> dict[str, Verdict]:
    pages = ok_pages(deps, run_id, lead_id)
    if not items or not pages:
        return {}
    chars = deps.settings.page_text_chars
    prompt, version = load_prompt("checklist_judge")
    msg = prompt.format(items=format_items(items), pages=format_pages(pages, chars))
    ctx = LLMContext(runs_db=deps.runs_db, settings=deps.settings, run_id=run_id, prompt_version=version,
                     ids=deps.ids, config=config)
    out = structured_call(deps.llms["checklist"], ChecklistOut, [HumanMessage(content=msg)], "checklist", ctx=ctx)
    if out is None:
        return {}
    texts = [(p["clean_text"] or "")[:chars] for p in pages]
    wanted = {i.id for i in items}
    verdicts: dict[str, Verdict] = {}
    for v in out.verdicts:
        if v.item_id not in wanted or v.item_id in verdicts:
            continue
        if v.value != VerdictValue.unknown and not quote_found(v.quote, texts):
            v = Verdict(item_id=v.item_id, value=VerdictValue.unknown)
        verdicts[v.item_id] = v
    return verdicts


def score_lead(deps: Deps, run_id: str, lead_id: str, plan: RunPlan,
               config: dict | None = None) -> tuple[int, list[Verdict]]:
    items = plan.checklist.items
    llm = llm_verdicts(deps, run_id, lead_id, [i for i in items if i.check == "llm"], config)
    verdicts = []
    for item in items:
        if item.check == "code" and item.code_check in CODE_CHECKS:
            verdicts.append(CODE_CHECKS[item.code_check](deps, run_id, lead_id, item))
        else:
            verdicts.append(llm.get(item.id) or Verdict(item_id=item.id, value=VerdictValue.unknown))
    return sum(v.value == VerdictValue.met for v in verdicts), verdicts
