"""classify_site (design item 32): one Haiku label per new domain, from its Exa title and snippet.

Cached in `triage_labels` by (domain_key, prompt_version), so a domain is never labelled twice for the same
prompt. A refusal or failed validation (structured_call returns None) is labelled `other`, which is rejected.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from langchain_core.messages import HumanMessage

from outreach.llm import LLMContext, load_prompt, structured_call
from outreach.models import TriageLabel, TriageOut

if TYPE_CHECKING:
    from outreach.deps import Deps


def classify_site(deps: Deps, run_id: str, domain_key: str, title: str, snippet: str,
                  config: dict | None = None) -> tuple[TriageLabel, float]:
    prompt, version = load_prompt("triage")
    cached = deps.runs_db.triage_get(domain_key, version)
    if cached is not None:
        return TriageLabel(cached["label"]), float(cached["confidence"] or 0.0)
    s = deps.settings
    msg = prompt.format(industry=deps.profile.industry, domain=domain_key, title=title[:200],
                        snippet=snippet[: s.snippet_chars])
    ctx = LLMContext(runs_db=deps.runs_db, settings=s, run_id=run_id, prompt_version=version, ids=deps.ids,
                     config=config)
    out = structured_call(deps.llms["triage"], TriageOut, [HumanMessage(content=msg)], "triage", ctx=ctx)
    result = out or TriageOut(label=TriageLabel.other, confidence=0.0, reason="no valid model output")
    deps.runs_db.triage_put(domain_key=domain_key, label=result.label, confidence=result.confidence,
                            reason=result.reason[:300], prompt_version=version)
    return result.label, result.confidence
