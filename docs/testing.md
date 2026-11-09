# Testing

```bash
python -m pytest -q            # or: make test
python -m ruff check .         # or: make lint (also checks formatting)
```

The suite needs no network, no accounts and no keys. Every HTTP request is answered by respx, SMTP by a fake server
object, and `tests/conftest.py` blocks sockets: a test that tries to reach the network fails with "tests must not
open network connections".

## What is covered

| File | What it tests |
|---|---|
| `test_config.py` | TOML, `.env` and environment precedence, secret keys rejected, validation errors |
| `test_logging.py` | JSON lines with job and run id, text format, library loggers |
| `test_state.py` | run history, seen keys, values, locks across connections, dry-run overlay, concurrent writers |
| `test_http.py` | retries and backoff, `Retry-After`, POST safety, timeouts, URLs kept out of logs |
| `test_fixtures.py` | the offline fixture transport and every example job run offline |
| `test_notify_console.py`, `test_notify_slack.py`, `test_notify_email.py` | each channel, the dispatcher, dry runs |
| `test_job_*.py` | each example job with mocked HTTP |
| `test_llm.py` | the Anthropic and OpenAI request formats, refusals and errors |
| `test_runner.py` | run records, failure alerts and their repeat window, recovery, locks, dry runs, discovery |
| `test_scheduler.py` | cron parsing, which jobs are due, no overlapping runs, graceful shutdown, SIGTERM |
| `test_cli.py` | every command through Typer's `CliRunner`, exit codes |

## Fixtures for your tests

From `tests/conftest.py`:

| Fixture | Gives you |
|---|---|
| `make_settings(toml_text)` | `Settings` from a TOML string (no `.env`, no environment) |
| `make_services(settings, registry=None, secrets=None, offline=False)` | the shared services for `run_job`, with a state database in the test's temporary folder |
| `recorders` | the notifiers `make_services` installs: `recorders["console"].messages` is what a job sent |
| `examples` | a copy of `examples/` in the working directory, for tests that use the example config |
| `now` | a fixed time (2026-10-01 07:00 UTC) to pass as `run_job(..., now=now)` |

Each test runs in an empty temporary working directory with the `AUTOMATION_*` and secret variables removed, so
your own `config.toml` and `.env` never leak into a test.

A job test usually mocks the HTTP calls, runs the job twice, and checks what was sent:

```python
import httpx
import respx

from automation.runner import run_job

FEED_XML = """<rss version="2.0"><channel><title>Example</title>
<item><title>Hello</title><link>https://example.com/hello</link><guid>hello</guid></item>
</channel></rss>"""


@respx.mock
def test_sends_new_items_once(make_settings, make_services, recorders, now):
    respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=FEED_XML))
    services = make_services(make_settings('[jobs.rss-digest]\nfeeds = ["https://example.com/feed.xml"]\n'))
    run_job(services, "rss-digest", now=now)
    run_job(services, "rss-digest", now=now)
    assert len(recorders["console"].messages) == 1
```

## Offline fixtures

`--offline` answers the jobs' HTTP requests from the JSON files in `examples/fixtures/` (the folder is
`fixtures_dir` in the config). One file per job is a good habit:

```json
{
  "description": "What these responses are for.",
  "routes": [
    {"method": "GET", "url": "https://example.com/", "status": 200, "body": "<html>...</html>"},
    {"method": "GET", "url": "https://api.example.com/items", "json": [{"id": 1}]},
    {"method": "GET", "url": "https://example.com/feed.xml", "body_file": "my-feed.xml",
     "headers": {"content-type": "application/rss+xml"}}
  ]
}
```

- `url` without a query string matches any query; with one, it must match exactly; `*` works as a wildcard.
- The body is one of `body` (text), `json` (any JSON value) or `body_file` (a file next to the JSON file; not a
  `.json` file, since every `.json` file in the folder is read as routes).
- Dates relative to the time of the request: `{{now}}`, `{{now-2d}}`, `{{now+6h}}`, `{{now-30m}}` become ISO 8601
  UTC timestamps, and `{{now-1d:rfc822}}` the RSS date format. That keeps time-window jobs, such as the last 7
  days of pull requests, returning results whenever you run them.
- A request with no matching route fails the run with "no fixture matches GET ...".

Notifications are not served from fixtures: in offline mode they still go out, through a separate live client.
Add `--dry-run` to send nothing.

## Testing against the real services

Keep live checks out of the test suite. Run a job by hand with your real config: `automation run <job> --dry-run`
shows what it would send, and without `--dry-run` it sends it.
