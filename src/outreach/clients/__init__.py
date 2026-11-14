"""Vendor clients (exa_client, hunter_client, mailer) and their shared error types."""

from __future__ import annotations


class VendorAuthError(Exception):
    """A vendor rejected the API key or billing (401/402). The run pauses."""

    def __init__(self, vendor: str, status: int, env_var: str) -> None:
        self.vendor, self.status, self.env_var = vendor, status, env_var
        super().__init__(f"{vendor} rejected the API key ({status}). Check {env_var} in .env, "
                         "then run `outreach resume`.")


class VendorUnavailableError(Exception):
    """A vendor kept failing after retries (429/5xx/connection). The run pauses."""

    def __init__(self, vendor: str, detail: str = "") -> None:
        self.vendor = vendor
        super().__init__(f"{vendor} is unavailable; run `outreach resume` later." + (f" ({detail})" if detail else ""))


class HunterQuotaError(Exception):
    """Hunter returned 429: the account's usage limit is reached. Never retried."""
