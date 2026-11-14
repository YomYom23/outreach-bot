"""The review screen (manual check, 2026-11-13): the email sits right above the prompt, draft text is never
read as Rich markup, and only the offered keys are accepted."""

from __future__ import annotations

import io

from rich.console import Console

from outreach.graph.pauses import review_keys
from outreach.models import PausePayload, ReviewAction
from outreach.ui.screens import Screens


def payload(decided: int) -> PausePayload:
    return PausePayload(kind="draft_review", pause_id="review:d1:1", prompt_text="Draft 1 of 2: Acme Roofing",
                        allowed_answers=[a.value for a in ReviewAction], context={
                            "draft_id": "d1", "shown_hash": "h", "subject": "Quick [b]idea[/b]",
                            "body": "Hi Jim,\n\nThanks [/link] for reading.\n\nSam Lee",
                            "verdicts": [{"value": "met", "text": "Family-owned", "quote": "family [owned]"}],
                            "points": 2, "threshold": 2, "email": "jim@acme.test", "email_type": "owner",
                            "email_source_kind": "page", "test_recipient": "inbox@outreach.test",
                            "keys": review_keys(decided)})


def render(p: PausePayload, inputs: list[str]) -> tuple[str, ReviewAction]:
    out = io.StringIO()
    screens = Screens(console=Console(file=out, width=100), read=lambda _prompt: inputs.pop(0))
    answer = screens.review(p)
    return out.getvalue(), answer.action


def test_email_is_last_before_the_keys_and_printed_as_plain_text() -> None:
    text, action = render(payload(0), ["a"])
    assert action == ReviewAction.approve
    assert "Thanks [/link] for reading." in text and "Quick [b]idea[/b]" in text and "family [owned]" in text
    assert text.index("Family-owned") < text.index("Hi Jim") < text.index("a approve")


def test_back_is_refused_when_not_offered() -> None:
    text, action = render(payload(0), ["b", "s"])
    assert action == ReviewAction.skip and "b back" not in text and "Type one of: a, e, r, s" in text
    _text, action = render(payload(1), ["b"])
    assert action == ReviewAction.back
