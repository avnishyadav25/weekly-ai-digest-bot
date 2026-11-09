from __future__ import annotations

import sys
import types
from datetime import timedelta

import pytest

from automation.config import ConfigError
from automation.jobs import Context, Job, JobConfig, JobFailed, Result, discover_jobs
from automation.runner import JobNotFound, run_job
from tests.conftest import NOW


class FlakyConfig(JobConfig):
    fail: bool = False


class Flaky(Job[FlakyConfig]):
    """A test job that fails when told to."""

    name = "flaky"
    config_model = FlakyConfig

    def run(self, ctx: Context[FlakyConfig]) -> Result:
        ctx.state.set("ran_at", ctx.now.isoformat())
        if ctx.config.fail:
            raise JobFailed("upstream returned garbage")
        return Result("all good", {"n": 1})


class Crashes(Job):
    name = "crashes"

    def run(self, ctx: Context) -> Result:
        raise KeyError("missing")


class Interrupted(Job):
    name = "interrupted"

    def run(self, ctx: Context) -> Result:
        raise KeyboardInterrupt


REGISTRY = {"flaky": Flaky, "crashes": Crashes, "interrupted": Interrupted}


@pytest.fixture
def services(make_settings, make_services):
    return make_services(
        make_settings("[jobs.flaky]\n[jobs.crashes]\n[jobs.interrupted]\n"), registry=dict(REGISTRY)
    )


def set_failing(services, fail: bool) -> None:
    services.settings.jobs["flaky"]["fail"] = fail


def test_success_is_recorded(services, recorders):
    outcome = run_job(services, "flaky", now=NOW)
    assert (outcome.status, outcome.summary, outcome.data) == ("success", "all good", {"n": 1})
    (run,) = services.store.runs("flaky")
    assert run.status == "success"
    assert run.summary == "all good"
    assert services.store.get_value("flaky", "ran_at") == NOW.isoformat()
    assert services.store.lock_info("flaky") is None  # released
    assert recorders["console"].messages == []


def test_failure_is_recorded_and_alerted(services, recorders):
    set_failing(services, True)
    outcome = run_job(services, "flaky", now=NOW)
    assert outcome.status == "failed"
    assert outcome.error == "upstream returned garbage"
    assert services.store.last_run("flaky").error == "upstream returned garbage"
    (alert,) = recorders["console"].messages
    assert alert.title == "flaky failed"
    assert alert.level == "error"
    assert alert.fields["failing since"] == "2026-10-01 07:00:00 UTC"


def test_unexpected_exceptions_name_their_type(services):
    outcome = run_job(services, "crashes", now=NOW)
    assert outcome.error == "KeyError: 'missing'"


def test_ctrl_c_records_the_run_and_releases_the_lock(services):
    with pytest.raises(KeyboardInterrupt):
        run_job(services, "interrupted", now=NOW)
    (run,) = services.store.runs("interrupted")
    assert (run.status, run.error) == ("failed", "interrupted")
    assert services.store.lock_info("interrupted") is None


def test_repeated_failures_alert_once_per_window_then_recovery(services, recorders):
    services.settings.notify.repeat_failure_alert_minutes = 60
    set_failing(services, True)
    for minutes in (0, 5, 30, 55):
        run_job(services, "flaky", now=NOW + timedelta(minutes=minutes))
    assert len(recorders["console"].messages) == 1
    run_job(services, "flaky", now=NOW + timedelta(minutes=61))
    assert len(recorders["console"].messages) == 2
    assert recorders["console"].messages[1].fields["failing since"] == "2026-10-01 07:00:00 UTC"
    set_failing(services, False)
    run_job(services, "flaky", now=NOW + timedelta(minutes=65))
    recovered = recorders["console"].messages[-1]
    assert recovered.title == "flaky recovered"
    assert "since 2026-10-01 07:00:00 UTC" in recovered.text
    set_failing(services, True)
    run_job(services, "flaky", now=NOW + timedelta(minutes=70))
    assert recorders["console"].messages[-1].fields["failing since"] == "2026-10-01 08:10:00 UTC"


def test_failure_alerts_use_on_failure_channels(make_settings, make_services, recorders):
    settings = make_settings(
        '[notify]\ndefault = ["console"]\non_failure = ["slack", "email"]\n[jobs.flaky]\nfail = true\n'
    )
    services = make_services(settings, registry=dict(REGISTRY))
    run_job(services, "flaky", now=NOW)
    assert recorders["console"].messages == []
    assert len(recorders["slack"].messages) == len(recorders["email"].messages) == 1


def test_overlapping_run_is_skipped(services):
    assert services.store.acquire_lock("flaky", "other-run", timedelta(minutes=60), NOW)
    outcome = run_job(services, "flaky", now=NOW + timedelta(minutes=1))
    assert outcome.status == "skipped"
    assert "previous run still running by run other-run" in outcome.summary
    assert services.store.runs("flaky") == []


def test_a_stale_lock_does_not_block_forever(services):
    services.store.acquire_lock("flaky", "dead-run", timedelta(minutes=60), NOW)
    assert run_job(services, "flaky", now=NOW + timedelta(minutes=61)).status == "success"


def test_disabled_job_is_skipped(make_settings, make_services):
    services = make_services(make_settings("[jobs.flaky]\nenabled = false\n"), registry=dict(REGISTRY))
    outcome = run_job(services, "flaky", now=NOW)
    assert outcome.status == "skipped"
    assert "disabled" in outcome.summary


def test_dry_run_writes_nothing_and_plans_the_failure_alert(services, recorders):
    set_failing(services, True)
    outcome = run_job(services, "flaky", now=NOW, dry_run=True)
    assert outcome.status == "failed"
    assert services.store.runs() == []
    assert services.store.values("flaky") == {}
    assert services.store.values("_runner") == {}
    assert recorders["console"].messages == []
    assert [p.message.title for p in outcome.planned_notifications] == ["flaky failed"]
    assert outcome.planned_state == [f'set ran_at = "{NOW.isoformat()}"']


def test_unknown_job_and_bad_config(services):
    with pytest.raises(JobNotFound, match="no job named 'nope'"):
        run_job(services, "nope")
    services.settings.jobs["flaky"]["unexpected"] = 1
    with pytest.raises(ConfigError, match="unexpected"):
        run_job(services, "flaky")


def test_discovery_finds_the_example_jobs_and_extra_modules(monkeypatch):
    assert list(discover_jobs()) == [
        "csv-report",
        "github-weekly",
        "rss-digest",
        "uptime-check",
        "weekly-ai-digest",
    ]
    module = types.ModuleType("my_jobs")

    class Hello(Job):
        name = "hello"

        def run(self, ctx: Context) -> Result:
            return Result("hi")

    Hello.__module__ = "my_jobs"
    module.Hello = Hello
    monkeypatch.setitem(sys.modules, "my_jobs", module)
    assert "hello" in discover_jobs(["my_jobs"])


def test_duplicate_job_names_are_an_error(monkeypatch):
    module = types.ModuleType("dupes")

    class Again(Job):
        name = "uptime-check"

    Again.__module__ = "dupes"
    module.Again = Again
    monkeypatch.setitem(sys.modules, "dupes", module)
    with pytest.raises(ValueError, match="two jobs are named 'uptime-check'"):
        discover_jobs(["dupes"])
