"""Phase 3 exit test: the draft editor (file format, marker, cancellation, early return, lookup)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from outreach.config import Settings
from outreach.ui.editor import MARKER, Editor, EditorNotFound, choose_editor, parse, render
from tests.conftest import make_settings


def which_of(found: dict[str, str]) -> Callable[[str], str | None]:
    return lambda name: found.get(name)


def test_file_format_and_parse_round_trip() -> None:
    text = render("Quick idea", "Hi Pat,\n\nBody.")
    assert text.startswith("Subject: Quick idea\n\nHi Pat,\n\nBody.\n" + MARKER + "\n")
    assert parse(text) == ("Quick idea", "Hi Pat,\n\nBody.")


def test_marker_and_line_endings() -> None:
    crlf = "\ufeffSubject: New\r\n\r\nLine one\r\nLine two\r\n" + MARKER + "\r\n# notes\r\nSubject: no\r\n"
    assert parse(crlf) == ("New", "Line one\nLine two")


@pytest.mark.parametrize(("env", "setting", "found", "expected"), [
    ({"VISUAL": "vim", "EDITOR": "nano"}, "", {"vim": "/bin/vim", "nano": "/bin/nano"}, ["/bin/vim"]),
    ({"VISUAL": "missing", "EDITOR": "nano"}, "", {"nano": "/bin/nano"}, ["/bin/nano"]),
    ({}, "subl -w", {"subl": "C:/subl.exe"}, ["C:/subl.exe", "-w"]),
    ({}, "", {"code": "C:/bin/code.cmd", "notepad": "C:/notepad.exe"}, ["C:/bin/code.cmd", "--wait"]),
    ({}, "", {"notepad": "C:/notepad.exe"}, ["C:/notepad.exe"]),
])
def test_editor_lookup_order(tmp_path: Path, env: dict, setting: str, found: dict, expected: list[str]) -> None:
    assert choose_editor(make_settings(tmp_path, editor=setting), env, which_of(found)) == expected


def test_no_editor_found(settings: Settings) -> None:
    with pytest.raises(EditorNotFound):
        choose_editor(settings, {}, which_of({}))


class FakeEditorRun:
    """Stands in for subprocess.run: rewrites the temp file (or not) and advances time."""

    def __init__(self, new_text: str | None, elapsed: float = 5.0, raise_ctrl_c: bool = False) -> None:
        self.new_text, self.elapsed, self.raise_ctrl_c = new_text, elapsed, raise_ctrl_c
        self.t = 0.0
        self.cmd: list[str] = []
        self.raw_bytes = b""

    def monotonic(self) -> float:
        return self.t

    def __call__(self, cmd: list[str], check: bool = False) -> None:
        self.cmd = cmd
        path = Path(cmd[-1])
        self.raw_bytes = path.read_bytes()
        self.t += self.elapsed
        if self.raise_ctrl_c:
            raise KeyboardInterrupt
        if self.new_text is not None:
            path.write_text(self.new_text, encoding="utf-8")


def make_editor(settings: Settings, run: FakeEditorRun, enter: Callable[[str], Any] = lambda _p: "") -> Editor:
    return Editor(settings, env={"EDITOR": "ed"}, which=which_of({"ed": "/bin/ed"}), run=run,
                  monotonic=run.monotonic, wait_for_enter=enter)


def test_saved_edit_is_returned_and_file_is_utf8_without_bom(settings: Settings) -> None:
    run = FakeEditorRun("Subject: Better\n\nNew body é\n" + MARKER + "\n")
    result = make_editor(settings, run).edit("Old", "Old body é")
    assert (result.subject, result.body, result.cancelled) == ("Better", "New body é", False)
    assert run.cmd[0] == "/bin/ed"
    assert not run.raw_bytes.startswith(b"\xef\xbb\xbf") and "é".encode() in run.raw_bytes


@pytest.mark.parametrize("new_text", [None, "", "   \n", "Subject: \n\n" + MARKER])
def test_unchanged_or_empty_file_cancels(settings: Settings, new_text: str | None) -> None:
    result = make_editor(settings, FakeEditorRun(new_text)).edit("Old", "Body")
    assert result.cancelled and result.subject is None


def test_early_return_waits_for_enter_then_rereads(settings: Settings) -> None:
    run = FakeEditorRun(None, elapsed=0.5)  # editor returned at once, file untouched
    prompts: list[str] = []

    def enter(prompt: str) -> str:
        prompts.append(prompt)
        Path(run.cmd[-1]).write_text("Subject: Later\n\nSaved in the other window\n", encoding="utf-8")
        return ""

    result = make_editor(settings, run, enter).edit("Old", "Body")
    assert prompts and (result.subject, result.body) == ("Later", "Saved in the other window")


def test_slow_unchanged_editor_does_not_prompt(settings: Settings) -> None:
    prompts: list[str] = []
    result = make_editor(settings, FakeEditorRun(None, elapsed=30), prompts.append).edit("Old", "Body")
    assert result.cancelled and prompts == []


def test_ctrl_c_keeps_temp_file(settings: Settings) -> None:
    editor = make_editor(settings, FakeEditorRun(None, raise_ctrl_c=True))
    result = editor.edit("Old", "Body")
    assert result.cancelled and result.kept_path is not None and result.kept_path.exists()
    assert str(result.kept_path) in editor.notices[0]
    result.kept_path.unlink()
