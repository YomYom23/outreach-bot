"""Make HTTPS work on machines whose antivirus or proxy intercepts TLS (docs/vendor_notes.md, Probe results).

Call `use_os_trust_store()` once, at the start of an entry point, before any HTTP client is built:

* It removes `SSLKEYLOGFILE` when the value is a Windows named pipe (`\\\\.\\...`). Norton Antivirus sets
  one, and OpenSSL in Python aborts writing keys to it ("no OPENSSL_Applink"). A real key-log file, set
  deliberately for Wireshark, is left alone.
* It makes `ssl` verify certificates against the operating system's store (truststore), as pip does.
  An intercepting antivirus or corporate proxy installs its root there, not in certifi's bundle.
  Verification stays on.

It patches `ssl` for the whole process, so call it only from an application's entry point.
"""

from __future__ import annotations

import os
from collections.abc import Callable, MutableMapping

PIPE_PREFIX = "\\\\.\\"


def drop_keylog_pipe(environ: MutableMapping[str, str]) -> bool:
    """Removes SSLKEYLOGFILE when it points at a named pipe. Returns True if it did."""
    if environ.get("SSLKEYLOGFILE", "").startswith(PIPE_PREFIX):
        del environ["SSLKEYLOGFILE"]
        return True
    return False


def use_os_trust_store(environ: MutableMapping[str, str] | None = None,
                       inject: Callable[[], None] | None = None) -> None:
    drop_keylog_pipe(os.environ if environ is None else environ)
    if inject is None:
        import truststore

        inject = truststore.inject_into_ssl
    inject()
