"""Rich screens for pauses, progress and the end screen (design item 49)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from outreach.models import PausePayload, PlanAnswer, ProgressEvent, QuestionAnswer, ReviewAction, ReviewAnswer
from outreach.ui.editor import Editor

REVIEW_KEYS = {"a": ReviewAction.approve, "e": ReviewAction.edit, "r": ReviewAction.reject, "s": ReviewAction.skip,
               "b": ReviewAction.back}


class Screens:
    def __init__(self, console: Console | None = None, editor: Editor | None = None,
                 read: Callable[[str], str] | None = None) -> None:
        self.console = console or Console()
        self.editor = editor
        self.read = read or (lambda prompt: self.console.input(prompt))

    # --- pauses -----------------------------------------------------------

    def ask(self, payload: PausePayload) -> PlanAnswer | QuestionAnswer | ReviewAnswer:
        if error := payload.context.get("error"):
            self.console.print(f"[red]{error}[/red]")
        if payload.kind == "plan":
            return self.plan(payload)
        if payload.kind == "question":
            return self.question(payload)
        return self.review(payload)

    def plan(self, payload: PausePayload) -> PlanAnswer:
        ctx = payload.context
        plan = ctx["plan"]
        table = Table(title=f"Run plan (version {plan['version']})", show_header=True)
        table.add_column("edit key")
        table.add_column("value")
        table.add_row("city", str(plan["city"]))
        table.add_row("target", str(plan["target_qualified"]))
        threshold = plan["checklist"]["threshold"]
        table.add_row("threshold", f"{threshold}" if threshold else "[red]not set (required)[/red]")
        for item in plan["checklist"]["items"]:
            check = f"code: {item['code_check']}" if item["check"] == "code" else "Claude reads the site"
            table.add_row(f"item.{item['id']}.text", f"{item['text']}  [dim]({check})[/dim]")
        for field, value in plan["sender"].items():
            if field != "signature":
                table.add_row(f"sender.{field}", str(value))
        table.add_row("sender.signature", ctx.get("signature", ""))
        self.console.print(table)
        self.console.print("Type [bold]key=value[/bold] to edit (also [bold]item.add=text[/bold], "
                           "[bold]item.<id>.remove=1[/bold]), [bold]remember sender[/bold], "
                           "[bold]remember checklist[/bold], or [bold]confirm[/bold].")
        remember: set[str] = set()
        while True:
            line = self.read("plan> ").strip()
            if line == "confirm":
                return PlanAnswer(pause_id=payload.pause_id, action="confirm", remember=remember)  # type: ignore[arg-type]
            if line.startswith("remember "):
                what = line.split(None, 1)[1].strip()
                if what in ("sender", "checklist"):
                    remember ^= {what}
                    self.console.print(f"remember: {', '.join(sorted(remember)) or 'nothing'}")
                    continue
            if "=" in line:
                key, value = line.split("=", 1)
                return PlanAnswer(pause_id=payload.pause_id, action="edit",
                                  edits={key.strip(): value.replace("\\n", "\n")})
            self.console.print("[yellow]Type key=value, remember sender|checklist, or confirm.[/yellow]")

    def question(self, payload: PausePayload) -> QuestionAnswer:
        ctx = payload.context
        self.console.print(Panel(payload.prompt_text, title="Question"))
        if explanation := ctx.get("explanation"):
            self.console.print(Panel(Text(explanation), border_style="cyan", title="Answer"))
        if suggestions := ctx.get("suggestions"):
            self.console.print("Suggestions: " + "; ".join(suggestions))
        hint = f" [{'/'.join(payload.allowed_answers)}]" if payload.allowed_answers else ""
        while not (value := self.read(f"answer{hint}> ").strip()):
            pass
        remember = False
        if ctx.get("can_remember"):
            remember = self.read("Remember this answer for future runs? [y/N] ").strip().lower() in ("y", "yes")
        return QuestionAnswer(pause_id=payload.pause_id, key=ctx["key"], value=value, remember=remember)

    def review(self, payload: PausePayload) -> ReviewAnswer:
        """The facts first, then the email, then the keys: the draft sits right above the prompt, so a short
        terminal can't scroll it out of view (manual check, 2026-11-13). Draft and page text is printed as
        plain text, never as Rich markup."""
        ctx = payload.context
        keys: dict[str, str] = ctx.get("keys") or {k: a.value for k, a in REVIEW_KEYS.items()}
        self.console.rule(escape(payload.prompt_text))
        for v in ctx.get("verdicts") or []:
            quote = f' "{v["quote"]}"' if v.get("quote") else ""
            self.console.print(Text(f"  {v.get('value', '?'):8} {v.get('text', v.get('item_id'))}{quote}"))
        self.console.print(Text(f"  points {ctx.get('points')} of N={ctx.get('threshold')}  |  "
                                f"{ctx.get('email')} ({ctx.get('email_type')}, {ctx.get('email_source_kind')})"
                                f"  ->  test inbox {ctx.get('test_recipient')}"))
        if flags := ctx.get("claim_flags"):
            self.console.print(Text(f"  check these words, not found in the evidence: {', '.join(flags)}",
                                    style="yellow"))
        email = Text.assemble(("Subject: ", "bold"), str(ctx["subject"]), "\n\n", str(ctx["body"]))
        self.console.print(Panel(email, title=escape(payload.prompt_text)))
        self.console.print("   ".join(f"[bold]{k}[/bold] {escape(label)}" for k, label in keys.items()))
        while True:
            key = self.read("review> ").strip().lower()[:1]
            action = REVIEW_KEYS.get(key) if key in keys else None
            if action is None:
                self.console.print(f"Type one of: {', '.join(keys)}")
                continue
            base = {"pause_id": payload.pause_id, "draft_id": ctx["draft_id"], "shown_hash": ctx["shown_hash"]}
            if action != ReviewAction.edit:
                return ReviewAnswer(**base, action=action)
            if self.editor is None:
                self.console.print("[red]No editor configured.[/red]")
                continue
            result = self.editor.edit(ctx["subject"], ctx["body"])
            for note in self.editor.notices:
                self.console.print(note)
            self.editor.notices.clear()
            if result.cancelled:
                self.console.print("Edit cancelled; nothing changed.")
                continue
            return ReviewAnswer(**base, action=action, subject=result.subject, body=result.body)

    # --- progress and end -------------------------------------------------

    def progress(self, chunk: Any) -> None:
        try:
            e = ProgressEvent.model_validate(chunk)
        except Exception:
            return
        where = f"round {e.round} " if e.round else ""
        self.console.print(f"[dim]{e.kind}[/dim] {where}{e.lead_id or ''} {e.status or ''}".rstrip())

    def end(self, data: dict[str, Any]) -> None:
        funnel = data["funnel"]
        table = Table(title="Run finished")
        for k in funnel:
            table.add_column(k)
        table.add_row(*(str(v) for v in funnel.values()))
        self.console.print(table)
        self.console.print(f"[bold]{data['summary']}[/bold]")
        if data["drop_reasons"]:
            self.console.print("Drop reasons: " + ", ".join(f"{k} {v}" for k, v in data["drop_reasons"].items()))
        u = data["usage"]
        self.console.print(f"Searches {u['searches']}, pages {u['pages']}, tokens {u['tokens']}, "
                           f"Hunter credits {u['hunter_credits']}, est. cost ${u['cost_estimate']:.4f}")
        for s in data.get("blocked_sends", []):
            self.console.print(f"[yellow]{s['status']}[/yellow] {s['domain_key']}: {s['reason']}")
        for a in data["assumptions"]:
            self.console.print(f"Assumed: {a}")

    def banner(self, run: Any, status: Any) -> None:
        self.console.print(Panel(
            f"Run {run['run_id']} ({run['status']}, router {run['router']}) started {run['created_at']}\n"
            f"Prompt: {run['prompt']}\n"
            f"{status.qualified_count} of {status.target_qualified} qualified, "
            f"{status.research_rounds_used} research rounds used",
            title="Unfinished run"))
