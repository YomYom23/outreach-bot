"""SandboxMailer: SMTP to the Mailtrap sandbox only (design item 46)."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from outreach.config import Settings
    from outreach.deps import Clock


class MailerRefused(Exception):
    """A host or recipient outside the sandbox allowlist."""


class SandboxMailer:
    def __init__(self, settings: Settings, user: str, password: str, *, clock: Clock,
                 last_send_at: Callable[[], datetime | None],
                 smtp_factory: Callable[..., Any] = smtplib.SMTP) -> None:
        if settings.mail_smtp_host not in settings.mail_host_allowlist:
            raise MailerRefused(f"SMTP host {settings.mail_smtp_host!r} is not in mail_host_allowlist")
        self.settings, self.user, self.password = settings, user, password
        self.clock, self.last_send_at, self.smtp_factory = clock, last_send_at, smtp_factory

    def _wait_for_interval(self) -> None:
        """Waits until mail_min_send_interval_s has passed since the latest send_log.finished_at.

        Read from runs.db, not memory, so it also holds after a resume.
        """
        last = self.last_send_at()
        if last is None:
            return
        elapsed = (self.clock.now() - last).total_seconds()
        remaining = self.settings.mail_min_send_interval_s - elapsed
        if remaining > 0:
            self.clock.sleep(remaining)

    def build_message(self, recipient: str, subject: str, body: str, headers: dict[str, str]) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = self.settings.mail_from
        msg["To"] = self.settings.mail_test_recipient
        msg["Subject"] = subject
        for name, value in headers.items():
            if name not in ("From", "To", "Subject") and value:
                msg[name] = value
        msg.set_content(body)
        return msg

    def send(self, recipient: str, subject: str, body: str, headers: dict[str, str]) -> str:
        """Returns the Message-ID. Envelope sender and recipient are always the configured test values."""
        if recipient != self.settings.mail_test_recipient:
            raise MailerRefused(f"recipient {recipient!r} is not mail_test_recipient")
        msg = self.build_message(recipient, subject, body, headers)
        self._wait_for_interval()
        s = self.settings
        with self.smtp_factory(s.mail_smtp_host, s.mail_smtp_port, timeout=s.connect_timeout_s + s.read_timeout_s) \
                as smtp:
            if s.mail_starttls:
                smtp.starttls()
            smtp.login(self.user, self.password)
            smtp.send_message(msg, from_addr=s.mail_from, to_addrs=[s.mail_test_recipient])
        return str(msg["Message-ID"] or "")
