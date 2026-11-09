"""Scheduling: cron expressions from the config, run in-process (APScheduler 3.x) or by an outside timer.

Two ways to run jobs on a schedule (docs/scheduling.md compares them):

- ``automation schedule``: a long-running process. APScheduler fires each job on its cron expression in a thread
  pool; ``max_instances=1`` means a job never overlaps itself (a fire time that arrives while the previous run is
  still going is skipped), ``coalesce`` merges missed fire times into one run, and SIGTERM/SIGINT stop the
  scheduler after the running jobs finish.
- ``automation schedule --once``: for cron, systemd timers, launchd and GitHub Actions. Runs every job whose cron
  expression has a fire time between its previous run and now, then exits.
"""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.events import EVENT_JOB_MAX_INSTANCES, JobSubmissionEvent
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from automation.config import ConfigError
from automation.runner import RunOutcome, Services, job_config, run_job

log = logging.getLogger("automation.scheduler")

RunFn = Callable[[Services, str], RunOutcome]


def make_trigger(expression: str, timezone: str = "UTC") -> CronTrigger:
    """A trigger from a 5-field cron expression (minute hour day month weekday)."""
    try:
        return CronTrigger.from_crontab(expression, timezone=ZoneInfo(timezone))
    except ValueError as exc:
        raise ConfigError(f"invalid cron expression {expression!r}: {exc}") from exc


@dataclass(frozen=True)
class Planned:
    job: str
    schedule: str
    trigger: CronTrigger


def scheduled_jobs(services: Services) -> list[Planned]:
    """Jobs with a ``[jobs.<name>]`` section, enabled, and a schedule (from the section or the job class)."""
    planned = []
    for name, job_cls in services.registry.items():
        if name not in services.settings.jobs:
            continue
        cfg = job_config(services, name)
        expression = cfg.schedule or job_cls.schedule
        if not cfg.enabled or not expression:
            continue
        planned.append(Planned(name, expression, make_trigger(expression, services.settings.timezone)))
    return planned


def _default_run(services: Services, name: str) -> RunOutcome:
    return run_job(services, name)


def is_due(trigger: CronTrigger, last_started: datetime | None, now: datetime) -> bool:
    """True when a fire time falls after ``last_started`` and at or before ``now`` (or the job never ran)."""
    if last_started is None:
        return True
    next_fire = trigger.get_next_fire_time(None, last_started + timedelta(microseconds=1))
    return next_fire is not None and next_fire <= now


def run_due(
    services: Services, *, now: datetime | None = None, run: RunFn = _default_run
) -> list[RunOutcome]:
    """``schedule --once``: run each job that is due, one after another."""
    now = now or datetime.now(UTC)
    outcomes = []
    for item in scheduled_jobs(services):
        last = services.store.last_run(item.job)
        if is_due(item.trigger, last.started_at if last else None, now):
            outcomes.append(run(services, item.job))
        else:
            log.debug("not due", extra={"job": item.job, "schedule": item.schedule})
    return outcomes


class Scheduler:
    """The in-process scheduler behind ``automation schedule``."""

    def __init__(self, services: Services, *, run: RunFn = _default_run, max_workers: int = 4) -> None:
        self.services = services
        self._run = run
        self.stop_event = threading.Event()
        self.scheduler = BackgroundScheduler(
            executors={"default": ThreadPoolExecutor(max_workers)},
            job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 300},
            timezone=ZoneInfo(services.settings.timezone),
        )
        self.scheduler.add_listener(self._on_skipped, EVENT_JOB_MAX_INSTANCES)

    def _on_skipped(self, event: JobSubmissionEvent) -> None:
        log.warning("skipped: previous run still running", extra={"job": event.job_id})

    def _call(self, name: str) -> None:
        try:
            self._run(self.services, name)
        except Exception:  # a config error in one job must not stop the scheduler
            log.exception("run could not start", extra={"job": name})

    def add_jobs(self) -> list[Planned]:
        planned = scheduled_jobs(self.services)
        for item in planned:
            self.scheduler.add_job(self._call, item.trigger, args=[item.job], id=item.job, name=item.job)
        return planned

    def start(self) -> list[Planned]:
        planned = self.add_jobs()
        self.scheduler.start()
        for item in planned:
            job = self.scheduler.get_job(item.job)
            next_run = job.next_run_time.isoformat() if job and job.next_run_time else None
            log.info("scheduled", extra={"job": item.job, "schedule": item.schedule, "next_run": next_run})
        return planned

    def stop(self, wait: bool = True) -> None:
        """Stop firing new runs; with ``wait`` (the default) block until running jobs finish."""
        self.stop_event.set()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)

    def run_forever(self, install_signal_handlers: bool = True) -> None:
        """Start, wait for SIGTERM/SIGINT (or :meth:`stop`), then shut down gracefully."""
        previous = {}
        if install_signal_handlers:
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous[sig] = signal.signal(sig, lambda signum, _frame: self._signal(signum))
        try:
            planned = self.start()
            if not planned:
                log.warning("no scheduled jobs: add a schedule to a [jobs.<name>] section")
            while not self.stop_event.wait(timeout=1.0):
                pass
            log.info("shutting down: waiting for running jobs to finish")
            self.stop(wait=True)
            log.info("scheduler stopped")
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)

    def _signal(self, signum: int) -> None:
        log.info("signal received", extra={"signal": signal.Signals(signum).name})
        self.stop_event.set()
