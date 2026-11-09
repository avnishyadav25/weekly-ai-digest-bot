from __future__ import annotations

import os
import signal
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from apscheduler.triggers.interval import IntervalTrigger

from automation.config import ConfigError
from automation.runner import RunOutcome
from automation.scheduler import Scheduler, is_due, make_trigger, run_due, scheduled_jobs
from tests.conftest import NOW

CONFIG = """
timezone = "UTC"
[jobs.uptime-check]
schedule = "*/5 * * * *"
urls = ["https://example.com/"]
[jobs.rss-digest]
feeds = ["https://example.com/feed.xml"]
[jobs.csv-report]
enabled = false
source = "x.csv"
group_by = "a"
"""


@pytest.fixture
def services(make_settings, make_services):
    return make_services(make_settings(CONFIG))


def test_cron_expressions_are_validated():
    assert make_trigger("0 9 * * mon-fri", "Europe/London")
    with pytest.raises(ConfigError, match="invalid cron expression"):
        make_trigger("every monday", "UTC")
    with pytest.raises(ConfigError):
        make_trigger("61 * * * *", "UTC")


def test_only_enabled_configured_jobs_with_a_schedule_are_planned(services):
    planned = {p.job: p.schedule for p in scheduled_jobs(services)}
    # rss-digest uses the job's default schedule; csv-report is disabled; github-weekly has no section
    assert planned == {"rss-digest": "0 7 * * *", "uptime-check": "*/5 * * * *"}


def test_is_due():
    trigger = make_trigger("*/5 * * * *")
    at = datetime(2026, 10, 1, 7, 0, 0, 300000, tzinfo=UTC)
    assert is_due(trigger, None, at)
    assert not is_due(trigger, at, at + timedelta(minutes=4, seconds=59))
    assert is_due(trigger, at, at + timedelta(minutes=5))
    assert is_due(trigger, at, at + timedelta(hours=6))  # missed runs collapse into one


def test_run_due_runs_what_is_due_and_records_it(services):
    ran: list[str] = []

    def fake_run(svc, name):
        ran.append(name)
        svc.store.start_run(name, f"{name}-{len(ran)}", NOW)
        return RunOutcome(name, "x", "success")

    run_due(services, now=NOW, run=fake_run)
    assert sorted(ran) == ["rss-digest", "uptime-check"]  # never ran before: both due
    ran.clear()
    run_due(services, now=NOW + timedelta(minutes=3), run=fake_run)
    assert ran == []
    run_due(services, now=NOW + timedelta(minutes=5), run=fake_run)
    assert ran == ["uptime-check"]


class SlowRun:
    """A run function that takes a while and tracks how many copies run at once."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.active = 0
        self.max_active = 0
        self.finished = 0
        self.lock = threading.Lock()

    def __call__(self, services, name) -> RunOutcome:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        time.sleep(self.seconds)
        with self.lock:
            self.active -= 1
            self.finished += 1
        return RunOutcome(name, "x", "success")


def test_a_job_never_overlaps_itself(services):
    slow = SlowRun(0.35)
    scheduler = Scheduler(services, run=slow)
    scheduler.scheduler.add_job(
        scheduler._call, IntervalTrigger(seconds=0.1), args=["uptime-check"], id="uptime-check"
    )
    scheduler.scheduler.start()
    time.sleep(1.2)
    scheduler.stop(wait=True)
    assert slow.finished >= 2
    assert slow.max_active == 1


def test_shutdown_waits_for_the_running_job(services):
    slow = SlowRun(0.4)
    scheduler = Scheduler(services, run=slow)
    scheduler.scheduler.add_job(scheduler._call, "date", run_date=datetime.now(UTC), args=["uptime-check"])
    scheduler.scheduler.start()
    deadline = time.monotonic() + 3
    while slow.active == 0:  # wait until the run has started
        assert time.monotonic() < deadline
        time.sleep(0.01)
    scheduler.stop(wait=True)
    assert slow.finished == 1


def test_sigterm_stops_run_forever_gracefully(services):
    scheduler = Scheduler(services, run=SlowRun(0))
    before = signal.getsignal(signal.SIGTERM)
    threading.Timer(0.3, os.kill, args=(os.getpid(), signal.SIGTERM)).start()
    started = time.monotonic()
    scheduler.run_forever()
    assert time.monotonic() - started < 3
    assert not scheduler.scheduler.running
    assert signal.getsignal(signal.SIGTERM) is before  # handlers restored


def test_scheduled_run_uses_the_real_runner(services):
    with respx.mock:
        respx.get("https://example.com/").mock(return_value=httpx.Response(200))
        scheduler = Scheduler(services)
        scheduler.scheduler.add_job(
            scheduler._call, "date", run_date=datetime.now(UTC), args=["uptime-check"]
        )
        scheduler.scheduler.start()
        deadline = time.monotonic() + 3
        while (
            not services.store.runs("uptime-check")
            or services.store.runs("uptime-check")[0].status == "running"
        ):
            assert time.monotonic() < deadline
            time.sleep(0.05)
        scheduler.stop()
    assert services.store.runs("uptime-check")[0].status == "success"
