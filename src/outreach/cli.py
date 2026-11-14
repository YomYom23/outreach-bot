"""The Typer app and the driver loop (design item 47).

Exit codes: 0 done, 1 error or vendor pause, 2 bad input, 3 config error, 4 version mismatch, 130 Ctrl+C.
"""

from __future__ import annotations

import re
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer
from langgraph.types import Command

from outreach import __version__
from outreach.clients import VendorAuthError, VendorUnavailableError
from outreach.config import ConfigError
from outreach.db import city_key
from outreach.db.memory_db import MigrationRefused
from outreach.deps import Deps
from outreach.models import PausePayload, RunStatusValue, StopReason

EXIT_OK, EXIT_ERROR, EXIT_BAD_INPUT, EXIT_CONFIG, EXIT_VERSION, EXIT_INTERRUPT = 0, 1, 2, 3, 4, 130
ROUTERS = ("claude", "code")
MISMATCH_TEXT = "answer did not match the current question; showing it again"

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Find, qualify and draft outreach to local "
                  "roofing companies. Mail goes to the Mailtrap test inbox only.")


@dataclass
class Runtime:
    deps: Deps
    build: Callable[[Deps], Any]  # Deps -> compiled graph
    screens: Any  # outreach.ui.screens.Screens or a scripted stand-in
    close: Callable[[], None] = field(default=lambda: None)

    def echo(self, text: str) -> None:
        self.screens.console.print(text)


def load_config() -> Any:
    """config.load(), then the tracing check. The order matters: the LangSmith key may live only in .env,
    and checking first would force LANGSMITH_TRACING=false, which load_dotenv(override=False) keeps."""
    from outreach import config
    from outreach.tracing import disable_tracing_if_unconfigured

    loaded = config.load()
    disable_tracing_if_unconfigured()
    return loaded


def make_runtime() -> Runtime:
    """Loads config and opens both databases. Tests replace this function."""
    from outreach.clients.exa_client import ExaClient
    from outreach.clients.hunter_client import HunterClient
    from outreach.clients.mailer import SandboxMailer
    from outreach.db.memory_db import MemoryDB
    from outreach.db.runs_db import RunsDB
    from outreach.deps import SystemClock, new_ulid
    from outreach.graph.build import build_graph
    from outreach.llm import build_llms
    from outreach.research.fetcher import Fetcher
    from outreach.tls import use_os_trust_store
    from outreach.ui.editor import Editor
    from outreach.ui.screens import Screens

    use_os_trust_store()  # before any HTTP client is built (outreach/tls.py)
    settings, profile, secrets = load_config()
    clock = SystemClock()
    runs_db = RunsDB(settings.runs_db_path, now=clock.now)
    memory_db = MemoryDB(settings.memory_db_path, now=clock.now, rejected_retry_days=settings.rejected_retry_days)
    mailer = SandboxMailer(settings, secrets.mailtrap_smtp_user, secrets.mailtrap_smtp_pass, clock=clock,
                           last_send_at=runs_db.last_send_finished_at)
    exa = ExaClient(secrets.exa_api_key, settings, exclude_domains=list(profile.directory_blocklist),
                    runs_db=runs_db, ids=new_ulid, sleep=clock.sleep)
    fetcher = Fetcher(settings, runs_db, clock)
    # Without a key the qualifier treats Hunter as unavailable: form-only leads end as `no_email`.
    hunter = (HunterClient(secrets.hunter_api_key, settings, runs_db=runs_db, ids=new_ulid, sleep=clock.sleep)
              if secrets.hunter_api_key else None)
    deps = Deps(settings=settings, profile=profile, runs_db=runs_db, memory_db=memory_db,
                llms=build_llms(settings, secrets), exa=exa, fetcher=fetcher, hunter=hunter, mailer=mailer,
                clock=clock)

    def close() -> None:
        fetcher.close()
        runs_db.close()
        memory_db.close()

    return Runtime(deps=deps, build=build_graph, screens=Screens(editor=Editor(settings)), close=close)


def _runtime() -> Runtime:
    try:
        return make_runtime()
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(EXIT_CONFIG) from e


# --- run lifecycle -------------------------------------------------------------------


def discard_run(rt: Runtime, run_id: str) -> None:
    """Always allowed, even across versions: status, registry claims, checkpoints."""
    db = rt.deps.runs_db
    db.set_run_status(run_id, RunStatusValue.discarded, stop_reason=StopReason.user_discarded.value)
    db.release_open_candidates(run_id)
    rt.deps.memory_db.release(run_id)
    db.saver.delete_thread(run_id)


def _migrate(rt: Runtime) -> None:
    try:
        rt.deps.memory_db.migrate(rt.deps.settings.runs_db_path)
    except MigrationRefused as e:
        rt.echo(f"[red]{e}[/red]")
        raise typer.Exit(EXIT_VERSION) from e


def _write_trace(rt: Runtime, run_id: str) -> Path | None:
    try:
        path = Path(rt.deps.settings.runs_db_path).parent / f"trace-{run_id}.txt"
        path.write_text(traceback.format_exc(), encoding="utf-8")
        return path
    except OSError:
        return None


def drive(rt: Runtime, run_id: str, inp: Any) -> int:
    """Streams until finalize or a pause; asks; resumes. Returns the exit code."""
    from outreach.graph.finalize import end_screen
    from outreach.tracing import flush, run_config

    deps = rt.deps
    db = deps.runs_db
    graph = rt.build(deps)
    last_answered: str | None = None
    db.set_run_status(run_id, RunStatusValue.active)
    try:
        while True:
            status = db.build_run_status(run_id, deps.settings, deps.memory_db.is_blocked_or_contacted)
            cfg = run_config(run_id, status.router, deps.settings, status)
            for _ns, mode, chunk in graph.stream(inp, cfg, durability="sync", stream_mode=["custom", "updates"],
                                                 subgraphs=True):
                if mode == "custom":
                    rt.screens.progress(chunk)
            interrupts = [i for t in graph.get_state(cfg).tasks for i in t.interrupts]
            if not interrupts:
                rt.screens.end(end_screen(deps, run_id))  # finalize wrote the final status
                return EXIT_OK
            payload = PausePayload.model_validate(interrupts[0].value)
            if payload.pause_id == last_answered and not {"error", "explanation"} & payload.context.keys():
                rt.echo(f"[yellow]{MISMATCH_TEXT}[/yellow]")
            answer = rt.screens.ask(payload)
            db.set_run_status(run_id, RunStatusValue.active)
            inp = Command(resume=answer.model_dump(mode="json"))
            last_answered = payload.pause_id
    except (VendorAuthError, VendorUnavailableError) as e:
        db.set_run_status(run_id, RunStatusValue.paused)
        rt.echo(f"[red]{e}[/red]")
        return EXIT_ERROR
    except KeyboardInterrupt:
        db.set_run_status(run_id, RunStatusValue.paused)
        rt.echo("Progress saved. Resume with `outreach resume`.")
        return EXIT_INTERRUPT
    except Exception as e:
        db.set_run_status(run_id, RunStatusValue.failed)
        trace = _write_trace(rt, run_id)
        rt.echo(f"[red]Run failed: {type(e).__name__}: {e}[/red]")
        rt.echo(f"Traceback saved to {trace}. Fix the cause, then run `outreach resume`.")
        return EXIT_ERROR
    finally:
        flush()


def _resume(rt: Runtime, run: Any, router: str | None) -> int:
    if run["app_version"] != __version__:
        rt.echo(f"[red]Run {run['run_id']} was made by version {run['app_version']}, this is {__version__}. "
                "It can't be resumed; `outreach discard` still works.[/red]")
        return EXIT_VERSION
    if router is not None and router != run["router"]:
        rt.echo(f"[red]Run {run['run_id']} uses --router {run['router']}; it can't change on resume.[/red]")
        return EXIT_BAD_INPUT
    _migrate(rt)
    rt.deps.runs_db.mark_sending_unknown(run["run_id"])
    return drive(rt, run["run_id"], None)


def _read_seeds(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def _finish(rt: Runtime, code: int) -> NoReturn:
    rt.close()
    raise typer.Exit(code)


# --- commands ------------------------------------------------------------------------


@app.command("run")
def run_cmd(
    prompt: Annotated[str | None, typer.Argument(help="What to look for, in plain words.")] = None,
    router: Annotated[str | None, typer.Option(help="claude or code")] = None,
    seeds: Annotated[Path | None, typer.Option(help="File with one domain per line; used in round 1.")] = None,
    city: Annotated[str | None, typer.Option(help="City to search, overriding the prompt.")] = None,
) -> None:
    """Start a run (or resume, start over, or discard an unfinished one)."""
    rt = _runtime()
    db = rt.deps.runs_db
    unfinished = db.unfinished_runs()
    if unfinished:
        run = unfinished[-1]
        rt.screens.banner(run, db.build_run_status(run["run_id"], rt.deps.settings))
        choice = rt.screens.read("Resume, start new, or discard? [resume/new/discard] ").strip().lower() or "resume"
        if choice not in ("resume", "new", "discard"):
            rt.echo("Type resume, new or discard.")
            _finish(rt, EXIT_BAD_INPUT)
        if choice == "resume":
            _finish(rt, _resume(rt, run, router))
        for r in unfinished:
            discard_run(rt, r["run_id"])
        rt.echo(f"Discarded {len(unfinished)} unfinished run(s).")
        if choice == "discard":
            _finish(rt, EXIT_OK)
    if not prompt:
        rt.echo("Give a prompt, for example: outreach run \"find me leads in Tucson that don't use AI\"")
        _finish(rt, EXIT_BAD_INPUT)
    router = router or rt.deps.settings.default_router
    if router not in ROUTERS:
        rt.echo(f"--router must be one of {', '.join(ROUTERS)}")
        _finish(rt, EXIT_BAD_INPUT)
    seed_list = None
    if seeds is not None:
        if not seeds.exists():
            rt.echo(f"Seeds file not found: {seeds}")
            _finish(rt, EXIT_BAD_INPUT)
        seed_list = _read_seeds(seeds)
    _migrate(rt)
    run_id = rt.deps.ids()
    db.create_run(run_id=run_id, app_version=__version__, router=router, prompt=prompt or "", seeds=seed_list,
                  city_override=city)
    _finish(rt, drive(rt, run_id, {"run_id": run_id}))


@app.command()
def resume(router: Annotated[str | None, typer.Option(help="Must match the run's router.")] = None) -> None:
    """Resume the unfinished run."""
    rt = _runtime()
    unfinished = rt.deps.runs_db.unfinished_runs()
    if not unfinished:
        rt.echo("No unfinished run to resume.")
        _finish(rt, EXIT_BAD_INPUT)
    _finish(rt, _resume(rt, unfinished[-1], router))


@app.command()
def discard() -> None:
    """Discard every unfinished run and release its claims."""
    rt = _runtime()
    unfinished = rt.deps.runs_db.unfinished_runs()
    for r in unfinished:
        discard_run(rt, r["run_id"])
    rt.echo(f"Discarded {len(unfinished)} unfinished run(s).")
    _finish(rt, EXIT_OK)


@app.command()
def leads(status: Annotated[str, typer.Option(help="parked, blocked or contacted")] = "parked") -> None:
    """List companies from memory.db."""
    rt = _runtime()
    if status not in ("parked", "blocked", "contacted"):
        rt.echo("--status must be parked, blocked or contacted")
        _finish(rt, EXIT_BAD_INPUT)
    _migrate(rt)  # a fresh install has no tables until the first migration
    rows = rt.deps.memory_db.list_leads(status)
    for r in rows:
        rt.echo(f"{r['domain_key']}  {r['city'] or '-'}  run {r['run_id']}  {r['at']}")
    rt.echo(f"{len(rows)} {status}")
    _finish(rt, EXIT_OK)


_DOMAIN_SHAPE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


def _purge_one(rt: Runtime, key: str) -> None:
    rt.deps.memory_db.purge(key)
    rt.deps.runs_db.purge_domain(key)


@app.command()
def purge(
    target: Annotated[str, typer.Argument(help="A domain key (acme-roofing.com) or a city (Tucson).")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation for a city purge.")] = False,
) -> None:
    """Forget a company, or every company found for a city, in both databases (do-not-contact included)."""
    rt = _runtime()
    if rt.deps.runs_db.unfinished_runs():
        rt.echo("A run is unfinished. Finish or discard it before purging.")
        _finish(rt, EXIT_BAD_INPUT)
    _migrate(rt)
    runs_db, memory_db = rt.deps.runs_db, rt.deps.memory_db

    # leads.city survives a runs.db wipe; the run plans also catch rows written before the city was recorded.
    city_runs = runs_db.runs_for_city(target)
    recorded = memory_db.domains_for_city(target)
    if city_runs or recorded:
        domains = sorted(recorded | runs_db.candidate_domains(city_runs) | memory_db.domains_for_runs(city_runs))
        if not domains:
            rt.echo(f"No companies recorded for {target.strip()}.")
            _finish(rt, EXIT_OK)
        rt.echo(f"{len(domains)} compan{'y' if len(domains) == 1 else 'ies'} found for {target.strip()}:")
        for d in domains:
            rt.echo(f"  {d}")
        if not yes and not typer.confirm("Purge all of them?", default=False):
            rt.echo("Nothing purged.")
            _finish(rt, EXIT_OK)
        for d in domains:
            _purge_one(rt, d)
        rt.echo(f"Purged {len(domains)} compan{'y' if len(domains) == 1 else 'ies'} for {target.strip()}.")
        _finish(rt, EXIT_OK)

    key = target.strip().lower()
    if not _DOMAIN_SHAPE.match(key):
        by_key = {city_key(c): c for c in [*memory_db.known_cities(), *runs_db.known_cities()]}
        cities = sorted(by_key.values(), key=str.lower)
        rt.echo(f"'{target.strip()}' is not a known city or a domain. "
                f"Known cities: {', '.join(cities) if cities else 'none yet'}.")
        _finish(rt, EXIT_BAD_INPUT)
    _purge_one(rt, key)
    rt.echo(f"Purged {key}.")
    _finish(rt, EXIT_OK)
