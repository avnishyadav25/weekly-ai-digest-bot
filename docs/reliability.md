# Reliability

What the project does so a job keeps working, does not repeat work, and tells you when it breaks; and what it
leaves to you.

## Retries

The HTTP client (`ctx.http`, [http.py](../src/automation/http.py)) retries failed requests with exponential backoff
and jitter: the first wait is `backoff_seconds` (0.5 s), then about twice as long each time, up to
`backoff_max_seconds` (30 s), for `retries` extra attempts (3).

| Failure | GET, HEAD, PUT, DELETE | POST, PATCH |
|---|---|---|
| Connection refused, DNS failure, connect timeout | retried | retried (the server never saw the request) |
| Read timeout, connection dropped mid-request | retried | not retried |
| 429 Too Many Requests | retried | retried |
| 408, 500, 502, 503, 504, 529 | retried | not retried |
| Other 4xx | not retried | not retried |

A POST that reached the server may have done its work (sent the message, created the record) even if the
response failed, so repeating it could do the work twice. Pass `retry_unsafe=True` when repeating is harmless (the
LLM calls do).

A `Retry-After` header (seconds or a date) sets the wait, capped at `backoff_max_seconds`. After the last attempt
the client returns the last response, or raises `httpx.HTTPStatusError` for a 4xx/5xx when `raise_for_status` is
on (the default), or re-raises the connection error.

## Timeouts

- **Each request:** `timeout_seconds` (10 s) for connecting, reading and writing; a job can pass `timeout=` per
  call (uptime-check has its own `timeout_seconds`).
- **Each run:** `max_runtime_minutes` (60) is how long a run may hold the job's lock. It does not kill the run;
  Python cannot safely stop a thread from the outside. To cap a run's wall time, let the platform do it:
  `TimeoutStartSec` in the systemd service (55 minutes in the recipe), `timeout-minutes` in GitHub Actions (15),
  or `timeout` in front of the command in cron.

## Overlapping runs

Every run takes the job's lock in the state database before it starts and releases it when it ends, so two runs of
the same job never overlap, even when they come from different processes (the scheduler, a cron tick, a manual
`automation run`). A run that finds the lock held is skipped with a warning and exit code 0.

If a run dies without releasing the lock (the process was killed, the machine rebooted), the lock expires after
`max_runtime_minutes`; the next run then takes it over and marks the dead run as `abandoned` in the history. Set
`max_runtime_minutes` comfortably above the job's longest normal run.

Dry runs take no lock.

## Idempotency

A job that runs again must not repeat work it already did. `ctx.state` keeps, per job:

- **Seen keys:** `unseen(keys)` returns the keys not processed yet, `mark_seen(keys)` records them. rss-digest uses
  feed item ids, github-weekly uses `owner/repo#number`, csv-report uses the period (`report:2026-W40`).
- **Values:** `get(key)` and `set(key, value)` for anything else, such as uptime-check's last known state per URL.

The example jobs mark items as done **after** the notification went out. If sending fails, the run fails, nothing
is marked, and the next run tries again. The trade-off: if the process dies between sending and marking, the next
run sends that message again. A repeated message is easier to notice and cheaper than a lost one.

`automation state show` shows what each job remembers; `automation state clear --job <name>` makes it start over.

## Alerts

- **Job messages** go to the job's `notify` channels (or `[notify] default`). uptime-check sends one message
  when a URL goes down and one when it recovers, not one per check.
- **Failure alerts:** when a run fails (an exception, or `JobFailed`), the runner sends "`<job>` failed" with the
  error to the `[notify] on_failure` channels. While the job keeps failing it repeats the alert at most every
  `repeat_failure_alert_minutes` (60), so a job that runs every 5 minutes does not send 12 alerts an hour.
- **Recovery:** the first successful run after an alert sends "`<job>` recovered" to the same channels.
- **Broken channels:** each channel is tried even if another failed. A failure alert that cannot be delivered
  anywhere is logged as an error, and the runner tries again on the next failed run.

What this project does not catch: the scheduler or the whole machine stopping. Nothing runs, so nothing alerts.
Cover that from outside: systemd's `OnFailure=` for the service, `restart: unless-stopped` in Docker, or a
heartbeat check in an external monitoring service that expects a request from a job on a schedule.

## Logs

One JSON object per line on stderr, with `ts` (UTC), `level`, `logger`, `event`, and `job` and `run_id` for
everything inside a run, plus the fields a job passes in `extra`. Collect them with `journalctl`, `docker logs` or
any log shipper that reads JSON lines; use `--log-format text` in a terminal.

Secrets stay out of the logs and alerts: settings hold them as `SecretStr`, request URLs are logged without query
strings, an HTTP error that fails a run is reported as "HTTP 500 from <URL without query>", the Slack webhook URL
is logged as `slack webhook`, and the `httpx` and `httpcore` loggers (which print full URLs) stay at WARNING even
with `--log-level DEBUG`.

## State

The state database is one SQLite file (`state/automation.db`) in WAL mode. Writes are short transactions, so the
scheduler's threads and separate processes can share it. Back it up with `sqlite3 state/automation.db ".backup
backup.db"`, or copy it while nothing runs. Deleting it resets every job: the next runs treat everything as new.
