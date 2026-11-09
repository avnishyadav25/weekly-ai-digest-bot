"""Run one job: validate its config, take its run lock, record the run, call ``job.run(ctx)``, alert on failure.

Used by ``automation run``, ``automation schedule`` and ``automation schedule --once``, so every way of running a
job gets the same lock, run history and failure alerts.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal, TextIO

import httpx
from pydantic import ValidationError

from automation.config import ConfigError, Secrets, Settings, format_validation_error
from automation.http import FixtureTransport, HttpClient, safe_url
from automation.jobs import Job, JobConfig, discover_jobs
from automation.jobs.base import Context, JobFailed
from automation.llm import LlmClient
from automation.logging import get_logger, log_context
from automation.notify import Dispatcher, Message, Notifier, NotifyError, PlannedNotification, build_notifiers
from automation.notify.email import SmtpFactory
from automation.state import JobState, StateStore

log = logging.getLogger("automation.runner")

Status = Literal["success", "failed", "skipped"]
RUNNER_STATE = "_runner"  # kv namespace for the runner's own bookkeeping (failure alerts)


class JobNotFound(Exception):
    pass


@dataclass
class Services:
    """Long-lived objects shared by every run in a process."""

    settings: Settings
    secrets: Secrets
    http: HttpClient  # what jobs use; serves fixtures in offline mode
    notify_http: HttpClient  # what notifiers use; always live
    store: StateStore
    notifiers: dict[str, Notifier]
    registry: dict[str, type[Job]]
    offline: bool = False

    def close(self) -> None:
        self.http.close()
        if self.notify_http is not self.http:
            self.notify_http.close()
        self.store.close()


def build_services(
    settings: Settings,
    secrets: Secrets,
    *,
    offline: bool = False,
    registry: dict[str, type[Job]] | None = None,
    store: StateStore | None = None,
    http: HttpClient | None = None,
    notify_http: HttpClient | None = None,
    console_stream: TextIO | None = None,
    smtp_factory: SmtpFactory | None = None,
) -> Services:
    if http is None:
        if offline:
            # Fixtures answer instantly and always the same way: retry, but without waiting.
            no_wait = settings.http.model_copy(update={"backoff_seconds": 0.0, "backoff_max_seconds": 0.0})
            http = HttpClient(
                no_wait, transport=FixtureTransport(settings.fixtures_dir), sleep=lambda _s: None
            )
        else:
            http = HttpClient(settings.http)
    if notify_http is None:
        notify_http = HttpClient(settings.http) if offline else http
    try:
        registry = registry if registry is not None else discover_jobs(settings.job_modules)
    except (ImportError, ValueError) as exc:
        raise ConfigError(f"job_modules: {exc}") from exc
    return Services(
        settings=settings,
        secrets=secrets,
        http=http,
        notify_http=notify_http,
        store=store or StateStore(settings.state.path),
        notifiers=build_notifiers(
            settings, secrets, notify_http, console_stream=console_stream, smtp_factory=smtp_factory
        ),
        registry=registry,
        offline=offline,
    )


def describe_error(exc: BaseException) -> str:
    """One line for the run record and the failure alert. HTTP errors name the URL without its query string,
    which may carry a token."""
    if isinstance(exc, (JobFailed, NotifyError)):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from {safe_url(exc.request.url)}"
    return f"{type(exc).__name__}: {exc}"


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def get_job(services: Services, name: str) -> type[Job]:
    try:
        return services.registry[name]
    except KeyError:
        known = ", ".join(services.registry) or "none"
        raise JobNotFound(f"no job named {name!r} (known: {known})") from None


def validate_section(job_cls: type[Job], settings: Settings, name: str) -> JobConfig:
    """The validated ``[jobs.<name>]`` section. Raises :class:`ConfigError` with the bad keys."""
    section = settings.jobs.get(name, {})
    try:
        return job_cls.config_model.model_validate(section)
    except ValidationError as exc:
        where = settings.config_file or "config"
        raise ConfigError(format_validation_error(exc, f"{where} [jobs.{name}]")) from exc


def job_config(services: Services, name: str) -> JobConfig:
    return validate_section(get_job(services, name), services.settings, name)


@dataclass
class RunOutcome:
    job: str
    run_id: str
    status: Status
    summary: str = ""
    error: str | None = None
    dry_run: bool = False
    seconds: float = 0.0
    data: dict[str, object] = field(default_factory=dict)
    planned_notifications: list[PlannedNotification] = field(default_factory=list)
    planned_state: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status != "failed"


def run_job(
    services: Services,
    name: str,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> RunOutcome:
    """Run ``name`` once. Never raises for a failing job (the outcome says ``failed``); raises
    :class:`JobNotFound` or :class:`ConfigError` before the run starts."""
    job_cls = get_job(services, name)
    cfg = job_config(services, name)
    run_id = new_run_id()
    now = now or datetime.now(UTC)
    if not cfg.enabled:
        return RunOutcome(
            name, run_id, "skipped", f"disabled in config ([jobs.{name}] enabled = false)", dry_run=dry_run
        )

    store = services.store
    with log_context(job=name, run_id=run_id):
        if not dry_run and not store.acquire_lock(
            name, run_id, timedelta(minutes=cfg.max_runtime_minutes), now
        ):
            info = store.lock_info(name)
            held = f" by run {info.owner} since {info.acquired_at:%Y-%m-%d %H:%M:%S} UTC" if info else ""
            log.warning(
                "skipped: previous run still holds the lock", extra={"holder": info.owner if info else None}
            )
            return RunOutcome(name, run_id, "skipped", f"previous run still running{held}")

        state = JobState(store, name, dry_run=dry_run, now=now)
        channels = cfg.notify or services.settings.notify.default
        notify = Dispatcher(services.notifiers, default=channels, dry_run=dry_run, job=name, run_id=run_id)
        ctx: Context[JobConfig] = Context(
            job=name,
            run_id=run_id,
            config=cfg,
            settings=services.settings,
            secrets=services.secrets,
            http=services.http,
            state=state,
            notify=notify,
            log=get_logger(f"jobs.{name}"),
            now=now,
            dry_run=dry_run,
            llm=LlmClient.from_settings(services.settings.llm, services.secrets, services.http),
        )
        outcome = RunOutcome(name, run_id, "success", dry_run=dry_run)
        started = time.monotonic()
        if not dry_run:
            store.start_run(name, run_id, now)
        log.info("run started", extra={"dry_run": dry_run, "offline": services.offline})
        try:
            result = job_cls().run(ctx)
            outcome.summary = result.summary
            outcome.data = dict(result.data)
        except Exception as exc:
            outcome.status = "failed"
            outcome.error = describe_error(exc)
            expected = isinstance(exc, (JobFailed, NotifyError, httpx.HTTPStatusError))
            log.error("run failed", extra={"error": outcome.error}, exc_info=not expected)
        except BaseException:  # Ctrl+C or SystemExit: record the run as failed, release the lock, then stop
            outcome.status = "failed"
            outcome.error = "interrupted"
            log.warning("run interrupted")
            raise
        finally:
            outcome.seconds = round(time.monotonic() - started, 3)
            if not dry_run:
                store.finish_run(
                    run_id,
                    outcome.status,
                    summary=outcome.summary or None,
                    error=outcome.error,
                    finished_at=now + timedelta(seconds=outcome.seconds),
                )
                store.release_lock(name, run_id)
        log.info(
            "run finished",
            extra={"status": outcome.status, "seconds": outcome.seconds, "summary": outcome.summary},
        )
        _after_run(services, outcome, notify, now)
        outcome.planned_notifications = notify.planned
        outcome.planned_state = state.planned
    return outcome


def _after_run(services: Services, outcome: RunOutcome, job_notify: Dispatcher, now: datetime) -> None:
    """Failure alert (repeated at most every ``repeat_failure_alert_minutes`` while the job keeps failing) and,
    once it succeeds again, a recovery message to the people who got the alert."""
    settings = services.settings.notify
    store = services.store
    since_key = f"{outcome.job}:failing_since"
    alert_key = f"{outcome.job}:last_alert"
    dispatcher = Dispatcher(
        services.notifiers,
        default=settings.on_failure,
        dry_run=outcome.dry_run,
        job=outcome.job,
        run_id=outcome.run_id,
    )
    failing_since = store.get_value(RUNNER_STATE, since_key)
    last_alert = store.get_value(RUNNER_STATE, alert_key)
    if outcome.status == "failed":
        since = failing_since or now.isoformat()
        if not outcome.dry_run and failing_since is None:
            store.set_value(RUNNER_STATE, since_key, since, now)
        repeat = timedelta(minutes=settings.repeat_failure_alert_minutes)
        if last_alert is not None and now - datetime.fromisoformat(last_alert) < repeat:
            log.info("failure alert suppressed (repeat window)", extra={"last_alert": last_alert})
            return
        message = Message(
            title=f"{outcome.job} failed",
            text=outcome.error or "unknown error",
            level="error",
            fields={"failing since": _short(since), "run": outcome.run_id},
        )
        _send_quietly(dispatcher, message)
        if dispatcher.sent and not outcome.dry_run:
            store.set_value(RUNNER_STATE, alert_key, now.isoformat(), now)
    elif outcome.status == "success" and failing_since:
        if last_alert is not None:
            message = Message(
                title=f"{outcome.job} recovered",
                text=f"The run succeeded again after failing since {_short(failing_since)}.",
                level="info",
            )
            _send_quietly(dispatcher, message)
        if not outcome.dry_run:
            forget_failures(store, outcome.job)
    job_notify.planned.extend(dispatcher.planned)


def _short(iso: str) -> str:
    return iso[:19].replace("T", " ") + " UTC"


def _send_quietly(dispatcher: Dispatcher, message: Message) -> None:
    try:
        dispatcher.send(message)
    except NotifyError as exc:
        log.error("alert could not be sent", extra={"error": str(exc)})


def forget_failures(store: StateStore, job: str | None = None) -> None:
    """Drop the runner's failure-alert bookkeeping for one job (or all jobs)."""
    if job is None:
        store.clear(RUNNER_STATE)
        return
    store.delete_value(RUNNER_STATE, f"{job}:failing_since")
    store.delete_value(RUNNER_STATE, f"{job}:last_alert")
