# Writing a job

A job is a class with a `name` and a `run(ctx)` method, in a module in `src/automation/jobs/`. The runner discovers
it, validates its config section, gives it a context (`ctx`) with everything it needs, records the run, and alerts
when it fails.

## A complete job in 20 lines of code

`src/automation/jobs/disk_space.py`:

```python
import shutil

from pydantic import Field

from automation.jobs.base import Context, Job, JobConfig, Result
from automation.notify import Message


class DiskSpaceConfig(JobConfig):
    path: str = "/"
    min_free_gb: float = Field(default=5, gt=0)


class DiskSpace(Job[DiskSpaceConfig]):
    name = "disk-space"
    description = "Alerts when free disk space drops below a threshold."
    schedule = "*/30 * * * *"
    config_model = DiskSpaceConfig

    def run(self, ctx: Context[DiskSpaceConfig]) -> Result:
        free_gb = shutil.disk_usage(ctx.config.path).free / 1e9
        low = free_gb < ctx.config.min_free_gb
        if low and not ctx.state.get("alerted"):
            text = f"{free_gb:.1f} GB free"
            ctx.notify.send(Message(f"Low disk on {ctx.config.path}", text, level="warning"))
        ctx.state.set("alerted", low)
        return Result(f"{free_gb:.1f} GB free")
```

Add a section to `config.toml` (the section is what turns a job on):

```toml
[jobs.disk-space]
path = "/"
min_free_gb = 10
```

Then `automation jobs list` shows it, `automation run disk-space --dry-run` tries it, and the scheduler runs it
every 30 minutes. The `alerted` value makes it alert once when space runs low and again only after it recovered.

Its test, `tests/test_job_disk_space.py`, uses the fixtures from `tests/conftest.py`:

```python
from collections import namedtuple

from automation.runner import run_job

Usage = namedtuple("Usage", "total used free")


def test_alerts_once_when_low(make_settings, make_services, recorders, monkeypatch):
    monkeypatch.setattr("shutil.disk_usage", lambda _p: Usage(100e9, 98e9, 2e9))
    services = make_services(make_settings("[jobs.disk-space]\nmin_free_gb = 5\n"))
    run_job(services, "disk-space")
    run_job(services, "disk-space")
    assert [m.title for m in recorders["console"].messages] == ["Low disk on /"]
```

## The parts of a job

| Part | What it is |
|---|---|
| `name` | The job's id: the CLI argument, the `[jobs.<name>]` section, the key in the state database. Lower-case with hyphens. |
| `description` | One line, shown by `automation jobs list`. |
| `schedule` | Default cron expression (minute hour day month weekday). `schedule` in the config section overrides it. `None` means manual only. |
| `config_model` | A `JobConfig` subclass. Its fields are the job's settings; unknown keys in the section are errors. Every job also has `enabled`, `schedule`, `notify` and `max_runtime_minutes`. |
| `run(ctx)` | Does the work and returns a `Result(summary, data)`. Raise `JobFailed("message")` to fail with a clear message; any other exception also fails the run, with its type in the alert. |

## What `ctx` gives you

| Attribute | Use it for |
|---|---|
| `ctx.config` | The validated config section (your `config_model`). |
| `ctx.http` | The shared HTTP client: timeouts, retries with backoff, `get_json`, `post_json`. Answers from fixtures under `--offline`. See [reliability.md](reliability.md#retries). |
| `ctx.state` | This job's memory in SQLite: `unseen(keys)`, `mark_seen(keys)`, `seen(key)`, `get(key)`, `set(key, value)`, `last_success()`. Writes nothing in a dry run. |
| `ctx.notify` | `send(Message(title, text, level, html=..., fields=..., link=...), channels=None)`. Default channels: the job's `notify`, else `[notify] default`. Records instead of sending in a dry run. |
| `ctx.log` | A logger; `ctx.log.info("event", extra={"key": value})` becomes one JSON line with the job name and run id. |
| `ctx.now` | The run's start time (UTC, timezone-aware). Use it instead of `datetime.now()` so tests can fix the time. |
| `ctx.secrets` | Secrets from the environment (`SecretStr`; call `.get_secret_value()`). |
| `ctx.llm` | The optional summariser (`None` unless `[llm]` and its key are set). |
| `ctx.dry_run`, `ctx.run_id`, `ctx.settings` | The run mode, the run id, the whole config. |

## Patterns from the example jobs

- **Process each item once** (rss-digest, github-weekly): build a stable key per item, ask `ctx.state.unseen(keys)`,
  act on those, then `ctx.state.mark_seen(keys)` after the action succeeded. If sending fails, the items stay
  unseen and the next run tries again.
- **Alert on change, not on every run** (uptime-check): keep the last state with `ctx.state.set`, compare, notify only
  on a transition, and save the new state after the message went out.
- **One report per period** (csv-report): mark a period key such as `report:2026-W40` as seen after sending, and
  skip the run when it is already there.
- **Fail with a clear message** (csv-report, github-weekly): turn expected problems (a missing column, a 404, a rate
  limit) into `JobFailed("what went wrong and what to do")`. The message is what the failure alert says.

## Offline fixtures for your job

If your job makes HTTP requests, add a fixture so `--offline` works for it: a JSON file in `examples/fixtures/`
with the canned responses. The format, and the `{{now-2d}}` date placeholders, are in
[testing.md](testing.md#offline-fixtures).

## Jobs outside this package

Put your jobs in your own package and list the modules in the config; they are discovered the same way:

```toml
job_modules = ["myjobs.billing", "myjobs.reports"]
```

The module must be importable (installed, or on `PYTHONPATH`). Two jobs with the same `name` are an error.
