"""Opens a draft in the user's text editor (design item 50). Runs in the CLI, never inside a node."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from outreach.config import Settings

MARKER = "#--- outreach: everything below this line is ignored ---"
INSTRUCTIONS = """# Edit the Subject line and the body above the marker, then save and close the editor.
# Saving counts as approval. An empty or unchanged file cancels the edit.
"""


class EditorNotFound(Exception):
    pass


def choose_editor(settings: Settings, env: Mapping[str, str] | None = None,
                  which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """VISUAL, then EDITOR, then settings.editor, then `code --wait`, then notepad.

    Each candidate's program is resolved with `which` (on Windows `code` resolves to `code.cmd`).
    """
    env = os.environ if env is None else env
    candidates = [env.get("VISUAL", ""), env.get("EDITOR", ""), settings.editor, "code --wait", "notepad"]
    for cand in candidates:
        if not cand.strip():
            continue
        parts = shlex.split(cand, posix=os.name != "nt")
        resolved = which(parts[0])
        if resolved:
            return [resolved, *parts[1:]]
    raise EditorNotFound("No text editor found. Set VISUAL or EDITOR, or `editor` in config/settings.toml.")


def render(subject: str, body: str) -> str:
    return f"Subject: {subject}\n\n{body}\n{MARKER}\n{INSTRUCTIONS}"


def parse(text: str) -> tuple[str, str]:
    """(subject, body) from the file text. Line endings are normalized; the marker and below are ignored."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    head = text.split(MARKER, 1)[0]
    lines = head.split("\n")
    subject = ""
    if lines and lines[0].lower().startswith("subject:"):
        subject = lines[0][len("subject:"):].strip()
        lines = lines[1:]
    return subject, "\n".join(lines).strip()


@dataclass
class EditResult:
    subject: str | None = None
    body: str | None = None
    cancelled: bool = False
    kept_path: Path | None = None


@dataclass
class Editor:
    settings: Settings
    env: Mapping[str, str] | None = None
    which: Callable[[str], str | None] = shutil.which
    run: Callable[..., Any] = subprocess.run
    monotonic: Callable[[], float] = time.monotonic
    wait_for_enter: Callable[[str], Any] = input
    notices: list[str] = field(default_factory=list)

    def edit(self, subject: str, body: str) -> EditResult:
        original = render(subject, body)
        fd, name = tempfile.mkstemp(prefix="outreach-draft-", suffix=".txt")
        path = Path(name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:  # UTF-8 without a BOM
            f.write(original)
        try:
            cmd = [*choose_editor(self.settings, self.env, self.which), str(path)]
            started = self.monotonic()
            self.run(cmd, check=False)
            text = path.read_text(encoding="utf-8")
            if self.monotonic() - started < self.settings.editor_early_return_s and text == original:
                # Some editors return at once and keep editing in another window.
                self.wait_for_enter("Press Enter when you have saved and closed the draft... ")
                text = path.read_text(encoding="utf-8")
        except KeyboardInterrupt:
            self.notices.append(f"Your edit is kept in {path}")
            return EditResult(cancelled=True, kept_path=path)
        new_subject, new_body = parse(text)
        if not text.strip() or text.replace("\r\n", "\n") == original or (new_subject, new_body) == ("", ""):
            path.unlink(missing_ok=True)
            return EditResult(cancelled=True)
        path.unlink(missing_ok=True)
        if not new_subject or not new_body:
            return EditResult(cancelled=True)
        return EditResult(subject=new_subject, body=new_body)
