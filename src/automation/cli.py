"""The ``automation`` command: jobs list | run <job> [--dry-run] | schedule [--once] | state show/clear | doctor.

Logs go to stderr (JSON lines by default); command output goes to stdout. Exit codes: 0 success (or nothing to
do), 1 a job failed or doctor found a problem, 2 a configuration or usage error.
"""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from automation import __version__
from automation.config import CHANNELS, ConfigError, Secrets, Settings, load_secrets, load_settings
from automation.http import FixtureError, load_fixtures
from automation.jobs import discover_jobs
from automation.llm import DEFAULT_MODELS, LlmClient
from automation.logging import setup_logging
from automation.notify import ConsoleNotifier
from automation.runner import (
    JobNotFound,
    RunOutcome,
    Services,
    build_services,
    forget_failures,
    run_job,
    validate_section,
)
from automation.scheduler import Scheduler, make_trigger, run_due
from automation.state import StateStore

app = typer.Typer(
    name="automation",
    help="Run scheduled Python jobs: list them, run one, run the scheduler, inspect their state.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
jobs_app = typer.Typer(help="List the jobs and their schedules.", no_args_is_help=True)
state_app = typer.Typer(help="Show or clear run history, seen keys and stored values.", no_args_is_help=True)
app.add_typer(jobs_app, name="jobs")
app.add_typer(state_app, name="state")

ConfigOpt = Annotated[
    Path | None,
    typer.Option(
        "--config",
        "-c",
        help="TOML config file. Default: $AUTOMATION_CONFIG, else ./config.toml.",
        show_default=False,
    ),
]
OfflineOpt = Annotated[
    bool,
    typer.Option(
        "--offline",
        help="Answer the jobs' HTTP requests from the fixtures folder. Notifications still "
        "go out unless you add --dry-run.",
    ),
]
LevelOpt = Annotated[
    str | None,
    typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR. Overrides the level in the config."),
]
FormatOpt = Annotated[
    str | None, typer.Option("--log-format", help="json or text. Overrides the format in the config.")
]


@dataclass
class Globals:
    config: Path | None = None
    offline: bool = False
    log_level: str | None = None
    log_format: str | None = None


def _version(value: bool) -> None:
    if value:
        typer.echo(f"automation {__version__}")
        raise typer.Exit()


@app.callback()
def main_options(
    ctx: typer.Context,
    config: ConfigOpt = None,
    offline: OfflineOpt = False,
    log_level: LevelOpt = None,
    log_format: FormatOpt = None,
    version: Annotated[
        bool, typer.Option("--version", callback=_version, is_eager=True, help="Show the version and exit.")
    ] = False,
) -> None:
    """Options given before the command apply to every command; the same options after it win."""
    ctx.obj = Globals(config, offline, log_level, log_format)


def _fail(message: str, code: int = 2) -> typer.Exit:
    typer.secho(message, err=True, fg=typer.colors.RED)
    return typer.Exit(code)


def _globals(
    ctx: typer.Context, config: Path | None, offline: bool, level: str | None, fmt: str | None
) -> Globals:
    base = ctx.obj if isinstance(ctx.obj, Globals) else Globals()
    return Globals(
        config=config or base.config,
        offline=offline or base.offline,
        log_level=level or base.log_level,
        log_format=fmt or base.log_format,
    )


def _load(opts: Globals) -> tuple[Settings, Secrets]:
    try:
        settings = load_settings(opts.config)
    except ConfigError as exc:
        raise _fail(f"config error: {exc}") from exc
    level = (opts.log_level or settings.log.level).upper()
    fmt = (opts.log_format or settings.log.format).lower()
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR"} or fmt not in {"json", "text"}:
        raise _fail("--log-level takes DEBUG, INFO, WARNING or ERROR; --log-format takes json or text")
    setup_logging(level, fmt)
    return settings, load_secrets()


def _services(opts: Globals, *, dry_run: bool = False) -> Services:
    settings, secrets = _load(opts)
    store = None
    if dry_run and not settings.state.path.exists():
        store = StateStore(":memory:")  # a dry run creates no files
    try:
        return build_services(settings, secrets, offline=opts.offline, store=store)
    except (ConfigError, FixtureError) as exc:
        if store is not None:
            store.close()
        raise _fail(f"config error: {exc}") from exc


# ---------------------------------------------------------------------------------------------------------------
# output helpers


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(str(c)) for c in col) for col in zip(headers, *rows, strict=False)]
    lines = ["  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines += ["  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip() for row in rows]
    return "\n".join(lines)


def _when(value: object) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S") if hasattr(value, "strftime") else "-"


_STATUS_COLOR = {"success": typer.colors.GREEN, "failed": typer.colors.RED, "skipped": typer.colors.YELLOW}


def _print_outcome(outcome: RunOutcome) -> None:
    if outcome.dry_run:
        typer.secho(f"Dry run of {outcome.job} (run {outcome.run_id}): nothing was sent or saved.", bold=True)
        console = ConsoleNotifier()
        if outcome.planned_notifications:
            for planned in outcome.planned_notifications:
                note = "" if planned.ready else f"  (not configured: {planned.note})"
                typer.echo(f"\nWould notify {planned.channel}:{note}")
                rendered = console.render(planned.message, job=outcome.job, run_id=outcome.run_id)
                typer.echo("".join(f"  {line}\n" for line in rendered.splitlines()).rstrip("\n"))
        else:
            typer.echo("\nWould notify: nobody (nothing to report)")
        if outcome.planned_state:
            typer.echo("\nWould save state:")
            for item in outcome.planned_state:
                typer.echo(f"  {item}")
        typer.echo("")
    status = typer.style(outcome.status, fg=_STATUS_COLOR[outcome.status], bold=True)
    detail = outcome.error if outcome.status == "failed" else outcome.summary
    typer.echo(
        f"{outcome.job}: {status} in {outcome.seconds:.2f}s (run {outcome.run_id}){': ' + detail if detail else ''}"
    )


# ---------------------------------------------------------------------------------------------------------------
# commands


@jobs_app.command("list")
def jobs_list(
    ctx: typer.Context,
    config: ConfigOpt = None,
    log_level: LevelOpt = None,
    log_format: FormatOpt = None,
) -> None:
    """List every job, whether it is configured, and its schedule."""
    settings, _ = _load(_globals(ctx, config, False, log_level, log_format))
    try:
        registry = discover_jobs(settings.job_modules)
    except (ImportError, ValueError) as exc:
        raise _fail(f"config error: job_modules: {exc}") from exc
    rows = []
    for name, job_cls in registry.items():
        schedule = job_cls.schedule or "manual"
        if name not in settings.jobs:
            status = "no config"
        else:
            try:
                cfg = validate_section(job_cls, settings, name)
                status = "enabled" if cfg.enabled else "disabled"
                schedule = cfg.schedule or schedule
            except ConfigError:
                status = "invalid config"
        rows.append([name, status, schedule, job_cls.description])
    unknown = sorted(set(settings.jobs) - set(registry))
    typer.echo(_table(["JOB", "STATUS", "SCHEDULE", "DESCRIPTION"], rows))
    if unknown:
        typer.secho(f"\nConfig sections without a job: {', '.join(unknown)}", fg=typer.colors.YELLOW)


@app.command()
def run(
    ctx: typer.Context,
    job: Annotated[
        str, typer.Argument(help="Job name, as shown by 'automation jobs list'.", show_default=False)
    ],
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run end to end without sending notifications or saving state; print what would happen.",
        ),
    ] = False,
    config: ConfigOpt = None,
    offline: OfflineOpt = False,
    log_level: LevelOpt = None,
    log_format: FormatOpt = None,
) -> None:
    """Run one job now."""
    services = _services(_globals(ctx, config, offline, log_level, log_format), dry_run=dry_run)
    try:
        outcome = run_job(services, job, dry_run=dry_run)
    except (JobNotFound, ConfigError) as exc:
        raise _fail(str(exc)) from exc
    finally:
        services.close()
    _print_outcome(outcome)
    raise typer.Exit(0 if outcome.ok else 1)


@app.command()
def schedule(
    ctx: typer.Context,
    once: Annotated[
        bool,
        typer.Option(
            "--once",
            help="Run the jobs that are due now, then exit (for cron, systemd timers, "
            "launchd and GitHub Actions).",
        ),
    ] = False,
    config: ConfigOpt = None,
    offline: OfflineOpt = False,
    log_level: LevelOpt = None,
    log_format: FormatOpt = None,
) -> None:
    """Run the scheduler: every job with a schedule, until stopped (Ctrl+C or SIGTERM)."""
    services = _services(_globals(ctx, config, offline, log_level, log_format))
    try:
        if once:
            outcomes = run_due(services)
            for outcome in outcomes:
                _print_outcome(outcome)
            if not outcomes:
                typer.echo("No job is due.")
            raise typer.Exit(0 if all(o.ok for o in outcomes) else 1)
        Scheduler(services).run_forever()
    except ConfigError as exc:
        raise _fail(f"config error: {exc}") from exc
    finally:
        services.close()


@state_app.command("show")
def state_show(
    ctx: typer.Context,
    job: Annotated[
        str | None, typer.Option("--job", "-j", help="Only this job, with its stored values.")
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n", help="How many recent runs to list.")] = 10,
    config: ConfigOpt = None,
) -> None:
    """Show recent runs, last successes, seen keys and stored values."""
    settings, _ = _load(_globals(ctx, config, False, None, None))
    path = settings.state.path
    if not path.exists():
        typer.echo(f"No state yet ({path} does not exist). Run a job first.")
        return
    store = StateStore(path)
    try:
        names = [job] if job else [n for n in store.jobs() if not n.startswith("_")]
        typer.secho(f"State: {path}\n", bold=True)
        stats = [store.job_stats(n) for n in names]
        typer.echo(
            _table(
                ["JOB", "RUNS", "LAST SUCCESS (UTC)", "SEEN KEYS", "VALUES"],
                [
                    [s["job"], str(s["runs"]), _when(s["last_success"]), str(s["seen"]), str(s["values"])]
                    for s in stats
                ],
            )
        )
        runs = store.runs(job, limit)
        if runs:
            typer.secho("\nRecent runs:", bold=True)
            rows = [
                [
                    _when(r.started_at),
                    r.job,
                    r.status,
                    f"{r.seconds:.2f}" if r.seconds is not None else "-",
                    (r.error or r.summary or "")[:70],
                ]
                for r in runs
            ]
            typer.echo(_table(["STARTED (UTC)", "JOB", "STATUS", "SECONDS", "SUMMARY"], rows))
        if job:
            values = store.values(job)
            typer.secho("\nStored values:", bold=True)
            for key, value in values.items():
                typer.echo(f"  {key} = {json.dumps(value, sort_keys=True)}")
            if not values:
                typer.echo("  (none)")
            lock = store.lock_info(job)
            if lock:
                typer.echo(f"\nLocked by run {lock.owner} until {_when(lock.expires_at)} UTC")
    finally:
        store.close()


@state_app.command("clear")
def state_clear(
    ctx: typer.Context,
    job: Annotated[
        str | None, typer.Option("--job", "-j", help="Only this job (default: every job).")
    ] = None,
    history: Annotated[
        bool, typer.Option("--history", help="Also delete run history and run locks.")
    ] = False,
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Forget seen keys and stored values, so the next run treats everything as new."""
    settings, _ = _load(_globals(ctx, config, False, None, None))
    path = settings.state.path
    if not path.exists():
        typer.echo(f"Nothing to clear ({path} does not exist).")
        return
    target = f"job {job}" if job else "every job"
    if not yes:
        typer.confirm(
            f"Forget seen keys and stored values{' and run history' if history else ''} for {target}?",
            abort=True,
        )
    store = StateStore(path)
    try:
        counts = store.clear(job, history=history)
        forget_failures(store, job)
    finally:
        store.close()
    parts = [f"{counts['seen']} seen key(s)", f"{counts['kv']} value(s)"]
    if history:
        parts.append(f"{counts['runs']} run(s)")
    typer.echo(f"Cleared {', '.join(parts)} for {target}.")


@app.command()
def doctor(
    ctx: typer.Context,
    config: ConfigOpt = None,
) -> None:
    """Check the config, the state path, the notification channels and the optional LLM settings."""
    opts = _globals(ctx, config, False, None, None)
    checks: list[tuple[str, str, str]] = []  # (status, name, detail): status in ok | warn | fail | off

    version = platform.python_version()
    checks.append(("ok" if sys.version_info >= (3, 11) else "fail", "python", version))
    try:
        settings = load_settings(opts.config)
    except ConfigError as exc:
        checks.append(("fail", "config", str(exc)))
        _print_checks(checks)
        raise typer.Exit(1) from None
    secrets = load_secrets()
    source = (
        str(settings.config_file)
        if settings.config_file
        else "no config file (defaults and environment only)"
    )
    checks.append(("ok" if settings.config_file else "warn", "config", source))
    checks.append(("ok", "timezone", settings.timezone))

    try:
        registry = discover_jobs(settings.job_modules)
    except (ImportError, ValueError) as exc:
        registry = {}
        checks.append(("fail", "jobs", f"job_modules: {exc}"))
    channels_used = set(settings.notify.default) | set(settings.notify.on_failure)
    for name in sorted(settings.jobs):
        if name not in registry:
            checks.append(("fail", "job", f"[jobs.{name}]: no job with this name"))
            continue
        job_cls = registry[name]
        try:
            cfg = validate_section(job_cls, settings, name)
            expression = cfg.schedule or job_cls.schedule
            if expression:
                make_trigger(expression, settings.timezone)
            channels_used |= set(cfg.notify or [])
            state = "disabled" if not cfg.enabled else (expression or "manual")
            checks.append(("ok", "job", f"{name}: {state}"))
        except ConfigError as exc:
            checks.append(("fail", "job", f"{name}: {exc}"))

    try:
        existed = settings.state.path.exists()
        store = StateStore(settings.state.path)
        store.check_writable()
        store.close()
        checks.append(
            ("ok", "state", f"{settings.state.path} is writable" + ("" if existed else " (created)"))
        )
    except Exception as exc:  # sqlite3.Error, OSError
        checks.append(("fail", "state", f"{settings.state.path}: {exc}"))

    from automation.http import HttpClient
    from automation.notify import build_notifiers

    with HttpClient(settings.http) as http:
        notifiers = build_notifiers(settings, secrets, http)
        for channel in CHANNELS:
            ready, note = notifiers[channel].configured()
            if ready:
                checks.append(("ok", channel, note))
            else:
                status = "fail" if channel in channels_used else "warn"
                used = " (used by the config)" if channel in channels_used else ""
                checks.append((status, channel, f"not configured: {note}{used}"))
        llm = settings.llm
        if llm.provider is None:
            checks.append(("off", "llm", "[llm] provider not set (summaries off)"))
        elif LlmClient.from_settings(llm, secrets, http) is None:
            key = "ANTHROPIC_API_KEY" if llm.provider == "anthropic" else "OPENAI_API_KEY"
            checks.append(("warn", "llm", f"{llm.provider}: set {key}"))
        else:
            checks.append(("ok", "llm", f"{llm.provider}, model {llm.model or DEFAULT_MODELS[llm.provider]}"))

    try:
        routes = load_fixtures(settings.fixtures_dir)
        checks.append(("ok", "fixtures", f"{settings.fixtures_dir}: {len(routes)} route(s) for --offline"))
    except FixtureError as exc:
        checks.append(("warn", "fixtures", f"{exc} (only needed for --offline)"))

    _print_checks(checks)
    raise typer.Exit(1 if any(status == "fail" for status, _, _ in checks) else 0)


_CHECK_COLOR = {"ok": typer.colors.GREEN, "warn": typer.colors.YELLOW, "fail": typer.colors.RED, "off": None}


def _print_checks(checks: list[tuple[str, str, str]]) -> None:
    width = max(len(name) for _, name, _ in checks)
    for status, name, detail in checks:
        label = typer.style(f"{status:<4}", fg=_CHECK_COLOR[status], bold=status != "off")
        typer.echo(f"{label}  {name.ljust(width)}  {detail}")
    failed = sum(1 for status, _, _ in checks if status == "fail")
    warned = sum(1 for status, _, _ in checks if status == "warn")
    typer.echo(f"\n{failed} problem(s), {warned} warning(s)")


def main() -> None:
    app()
