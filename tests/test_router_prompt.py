"""The router prompt never contains page text (design item 57; Supervisor contract: RunStatus input).

A whole run on the real researcher and qualifier over the fixture sites, with the Claude router. Every
prompt the router model saw is checked for anything that came from a web page or a model's reading of one.
"""

from __future__ import annotations

from pathlib import Path

from outreach.graph.build import build_graph
from outreach.graph.routers import router_prompt
from outreach.graph.supervisor import legal_moves
from tests.fakes import RecordingMailer
from tests.helpers import Driver, drafter_chat, parse_chat, router_chat
from tests.qualify_support import ChecklistJudge
from tests.sites import research_env
from tests.test_legal_moves import st

PAGE_FRAGMENTS = [
    "We install, repair", "family-owned", "Jim Carter", "Ortega", "acme-roofing", "@", "Tucson's",
    "IMPORTANT SYSTEM NOTICE", "page_content", "Speedway", "homeowners",
]


def test_router_prompts_hold_counts_only(tmp_path: Path) -> None:
    env = research_env(tmp_path)
    deps = env.deps
    router = router_chat()
    judge = ChecklistJudge()
    judge.by_domain["cf-roofing.test"] = {"i2": ("not_met", None), "i3": ("not_met", None)}  # 3 of 4 qualify:
    # the router then chooses between more research and drafting
    deps.llms.update(parse=parse_chat(), drafter=drafter_chat(), router=router, checklist=judge.chat)
    deps.mailer = RecordingMailer()
    deps.llms["discovery"].responder = lambda _m: "No new searches."  # later rounds: stop at once
    try:
        d = Driver(build_graph(deps), deps, run_id="R2")
        d.create_run(router="claude")
        p = d.through_intake(d.start())
        while p is not None and p.kind == "draft_review":
            p = d.review(p, "approve")
        assert p is None
        assert judge.chat.call_count > 0 and deps.llms["judge"].call_count > 0  # pages really were read
        assert router.call_count > 0
        prompts = [m.content for call in router.calls for m in call]
        for fragment in PAGE_FRAGMENTS:
            assert not any(fragment in text for text in prompts), fragment
        assert all('"qualified_count"' in text and "Legal moves:" in text for text in prompts)
    finally:
        env.close()


def test_prompt_lists_only_legal_moves_and_available_keys() -> None:
    from tests.test_legal_moves import ASKABLE

    status = st(**ASKABLE)
    text = router_prompt(status, legal_moves(status))
    assert "Legal moves: ask, finish" in text
    assert "Question keys you may use: change_city, extra_round" in text
    assert "Questions left: 3" in text
    quiet = st(undrafted_qualified=1, qualified_count=1)
    assert "Question keys you may use: (none" in router_prompt(quiet, legal_moves(quiet))
