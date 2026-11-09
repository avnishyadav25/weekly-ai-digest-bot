from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from automation import __version__
from automation.cli import app
from automation.state import StateStore

runner = CliRunner()
CONFIG = "examples/config.example.toml"


def invoke(*args: str, input: str | None = None):
    return runner.invoke(app, list(args), input=input)


def test_help_lists_the_commands():
    result = invoke("--help")
    assert result.exit_code == 0
    for command in ("run", "schedule", "doctor", "jobs", "state"):
        assert command in result.stdout


def test_version():
    assert invoke("--version").stdout.strip() == f"automation {__version__}"


def test_jobs_list(examples):
    result = invoke("jobs", "list", "--config", CONFIG)
    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].split()[:3] == ["JOB", "STATUS", "SCHEDULE"]
    assert any(
        line.startswith("uptime-check") and "enabled" in line and "*/5 * * * *" in line for line in lines
    )
    assert len(lines) == 6


def test_jobs_list_without_config_shows_defaults():
    result = invoke("jobs", "list")
    assert result.exit_code == 0
    assert "no config" in result.stdout


def test_dry_run_offline_prints_the_plan_and_creates_nothing(examples, tmp_path):
    result = invoke("run", "uptime-check", "--dry-run", "--offline", "--config", CONFIG)
    assert result.exit_code == 0, result.output
    assert "Dry run of uptime-check" in result.stdout
    assert "Would notify console:" in result.stdout
    assert "https://status.example.com/health is down: HTTP 503" in result.stdout
    assert "Would save state:" in result.stdout
    assert not (tmp_path / "state").exists()
    events = [json.loads(line)["event"] for line in result.stderr.splitlines()]
    assert events[0] == "run started"
    assert events[-1] == "run finished"


def test_global_options_work_before_the_command(examples):
    result = invoke("--offline", "--config", CONFIG, "--log-format", "text", "run", "csv-report")
    assert result.exit_code == 0, result.output
    assert "Weekly orders by region" in result.stdout
    assert "INFO" in result.stderr and not result.stderr.startswith("{")


def test_real_run_then_state_show_and_clear(examples):
    assert invoke("run", "rss-digest", "--offline", "-c", CONFIG).exit_code == 0
    again = invoke("run", "rss-digest", "--offline", "-c", CONFIG)
    assert "no new items" in again.stdout
    show = invoke("state", "show", "-c", CONFIG)
    assert show.exit_code == 0
    assert "rss-digest" in show.stdout
    assert "3 new items sent" in show.stdout
    detail = invoke("state", "show", "--job", "rss-digest", "-c", CONFIG)
    assert "Stored values:" in detail.stdout
    refused = invoke("state", "clear", "--job", "rss-digest", "-c", CONFIG, input="n\n")
    assert refused.exit_code == 1
    cleared = invoke("state", "clear", "--job", "rss-digest", "--yes", "-c", CONFIG)
    assert "Cleared 3 seen key(s), 0 value(s) for job rss-digest." in cleared.stdout
    assert "3 new items sent" in invoke("run", "rss-digest", "--offline", "-c", CONFIG).stdout


def test_failing_job_exits_1(examples):
    (examples / "fixtures" / "rss-digest-feed.xml").write_text("<html>gone</html>", encoding="utf-8")
    result = invoke("run", "rss-digest", "--offline", "-c", CONFIG)
    assert result.exit_code == 1
    assert "rss-digest: failed" in result.stdout
    assert "[ALERT] rss-digest failed" in result.stdout  # the console failure alert


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["run", "nope", "-c", CONFIG], "no job named 'nope'"),
        (["run", "uptime-check", "-c", "missing.toml"], "config file not found"),
        (["run", "uptime-check", "-c", CONFIG, "--log-level", "LOUD"], "--log-level takes"),
    ],
)
def test_usage_and_config_errors_exit_2(examples, args, message):
    result = invoke(*args)
    assert result.exit_code == 2
    assert message in result.stderr


def test_offline_without_fixtures_is_a_config_error(write_config):
    write_config('fixtures_dir = "nowhere"\n[jobs.uptime-check]\nurls = ["https://example.com/"]\n')
    result = invoke("run", "uptime-check", "--offline", "--dry-run")
    assert result.exit_code == 2
    assert "fixtures folder not found" in result.stderr


def test_schedule_once_runs_due_jobs_then_nothing(examples):
    first = invoke("schedule", "--once", "--offline", "-c", CONFIG)
    assert first.exit_code == 0, first.output
    for job in ("uptime-check", "rss-digest", "csv-report", "github-weekly"):
        assert f"{job}: success" in first.stdout
    second = invoke("schedule", "--once", "--offline", "-c", CONFIG)
    assert second.stdout.strip() == "No job is due."
    with StateStore("state/automation.db") as store:
        assert len(store.runs()) == 5


def test_doctor_reports_channels(examples, monkeypatch):
    result = invoke("doctor", "-c", CONFIG)
    assert result.exit_code == 0, result.output
    assert "warn  slack" in result.stdout
    assert "not configured: set SLACK_WEBHOOK_URL" in result.stdout
    assert "examples/fixtures: 7 route(s)" in result.stdout
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "https://example.com/hook")
    assert "ok    slack" in invoke("doctor", "-c", CONFIG).stdout


def test_doctor_fails_when_a_used_channel_is_not_configured(write_config):
    write_config('[notify]\non_failure = ["slack"]\n')
    result = invoke("doctor")
    assert result.exit_code == 1
    assert "fail  slack" in result.stdout
    assert "(used by the config)" in result.stdout


def test_doctor_reports_bad_job_sections(write_config):
    write_config("[jobs.uptime-check]\nurls = []\n[jobs.typo-job]\n")
    result = invoke("doctor")
    assert result.exit_code == 1
    assert "[jobs.typo-job]: no job with this name" in result.stdout
    assert "uptime-check" in result.stdout


def test_state_show_before_any_run():
    assert "No state yet" in invoke("state", "show").stdout
