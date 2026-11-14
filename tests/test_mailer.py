"""Phase 3 exit test: SandboxMailer refuses anything but the sandbox, sets headers, and paces sends."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from outreach.clients.mailer import MailerRefused, SandboxMailer
from outreach.config import Settings
from tests.conftest import make_settings
from tests.fakes import FakeClock


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float) -> None:
        self.host, self.port, self.timeout = host, port, timeout
        self.log: list[tuple[str, Any]] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.log.append(("quit", None))

    def starttls(self) -> None:
        self.log.append(("starttls", None))

    def login(self, user: str, password: str) -> None:
        self.log.append(("login", user))

    def send_message(self, msg: Any, from_addr: str, to_addrs: list[str]) -> None:
        self.log.append(("send", (msg, from_addr, to_addrs)))


@pytest.fixture(autouse=True)
def _reset() -> None:
    FakeSMTP.instances = []


def mailer(settings: Settings, clock: FakeClock, last: Any = None) -> SandboxMailer:
    return SandboxMailer(settings, "user", "pass", clock=clock, last_send_at=lambda: last, smtp_factory=FakeSMTP)


HEADERS = {"From": "ignored@x.test", "To": "ignored@x.test", "X-Intended-Recipient": "owner@acme-roofing.test",
           "Message-ID": "<draft-d1@outreach-bot.local>"}


def test_refuses_host_off_allowlist(tmp_path: Any, clock: FakeClock) -> None:
    with pytest.raises(MailerRefused, match="smtp.gmail.com"):
        mailer(make_settings(tmp_path, mail_smtp_host="smtp.gmail.com"), clock)


def test_refuses_other_recipients(settings: Settings, clock: FakeClock) -> None:
    m = mailer(settings, clock)
    with pytest.raises(MailerRefused):
        m.send("owner@acme-roofing.test", "s", "b", HEADERS)
    assert FakeSMTP.instances == []


def test_headers_envelope_and_tls(settings: Settings, clock: FakeClock) -> None:
    message_id = mailer(settings, clock).send(settings.mail_test_recipient, "Hello", "Body text", HEADERS)
    [smtp] = FakeSMTP.instances
    assert (smtp.host, smtp.port) == ("sandbox.smtp.mailtrap.io", 2525)
    assert [k for k, _ in smtp.log] == ["starttls", "login", "send", "quit"]
    msg, from_addr, to_addrs = smtp.log[2][1]
    assert (from_addr, to_addrs) == (settings.mail_from, [settings.mail_test_recipient])
    assert msg["From"] == settings.mail_from and msg["To"] == settings.mail_test_recipient
    assert msg["Subject"] == "Hello"
    assert msg["X-Intended-Recipient"] == "owner@acme-roofing.test"
    assert msg["Message-ID"] == message_id == "<draft-d1@outreach-bot.local>"
    assert msg.get_content().strip() == "Body text"


@pytest.mark.parametrize(("seconds_ago", "slept"), [(None, []), (3, [7.0]), (10, []), (25, [])])
def test_pacing_reads_last_send_time(settings: Settings, clock: FakeClock, seconds_ago: int | None,
                                     slept: list[float]) -> None:
    last = None if seconds_ago is None else clock.now() - timedelta(seconds=seconds_ago)
    mailer(settings, clock, last).send(settings.mail_test_recipient, "s", "b", HEADERS)
    assert clock.slept == slept
