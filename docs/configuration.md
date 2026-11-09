# Configuration

Settings live in a TOML file; secrets live in the environment. Nothing else is needed: every setting has a default,
and a job runs once it has a `[jobs.<name>]` section.

## Where settings come from

Highest priority first:

1. Environment variables (`AUTOMATION_<SECTION>__<KEY>`, see [Overrides](#overrides-from-the-environment)).
2. The `.env` file in the directory you run `automation` from.
3. The TOML file: `--config <file>`, else the file named by `AUTOMATION_CONFIG`, else `config.toml` in the current
   directory if it exists.
4. The defaults in the tables below.

Relative paths in the file (`state.path`, `fixtures_dir`, a CSV `source`) are relative to the directory you run
`automation` from. The deploy recipes set that directory for you (`WorkingDirectory` in systemd and launchd, `cd`
in cron, `/app` in Docker).

Unknown keys in the file are errors, in every section, so a typo does not silently fall back to a default; a job
section whose name matches no job is reported by `automation doctor` and `automation jobs list`. `automation doctor`
validates the whole file, including every job section and cron expression.

## Secrets

Secrets are read only from the environment or `.env`, never from the TOML file. A key in the TOML file whose name
ends in `password`, `secret`, `token`, `api_key`, `webhook` or `webhook_url` (and similar) is rejected with an error,
so a secret cannot end up in version control by accident. `max_tokens` is fine.

| Variable | Used by |
|---|---|
| `SLACK_WEBHOOK_URL` | The `slack` channel (an incoming webhook URL). |
| `SMTP_PASSWORD` | The `email` channel, with `[notify.email] username`. |
| `GITHUB_TOKEN` | github-weekly: private repositories and the higher rate limit. |
| `ANTHROPIC_API_KEY` | `[llm] provider = "anthropic"`. |
| `OPENAI_API_KEY` | `[llm] provider = "openai"`. |

Secrets are held as `SecretStr` values: they print as `**********` and never appear in logs.

## Top level

| Key | Default | Meaning |
|---|---|---|
| `timezone` | `"UTC"` | IANA time zone for the cron schedules, e.g. `"Europe/London"`. Timestamps in logs and state are UTC. |
| `fixtures_dir` | `"examples/fixtures"` | Canned HTTP responses used with `--offline`. |
| `job_modules` | `[]` | Extra modules that contain `Job` classes, e.g. `["myjobs.billing"]`. |

## [log]

| Key | Default | Meaning |
|---|---|---|
| `level` | `"INFO"` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. `--log-level` overrides it. |
| `format` | `"json"` | `json` (one object per line, for log collectors) or `text` (for a terminal). `--log-format` overrides it. |

## [state]

| Key | Default | Meaning |
|---|---|---|
| `path` | `"state/automation.db"` | The SQLite file with run history, run locks, seen keys and stored values. Created on first use. |

## [http]

| Key | Default | Meaning |
|---|---|---|
| `timeout_seconds` | `10` | Connect, read and write timeout per request. |
| `retries` | `3` | Extra attempts for timeouts, connection errors, 429 and 5xx. `0` turns retries off. |
| `backoff_seconds` | `0.5` | First wait between attempts; it doubles each time, with jitter. |
| `backoff_max_seconds` | `30` | Longest wait, including a server's `Retry-After`. |
| `user_agent` | `"python-automation/1.0"` | Sent with every request. Some APIs ask for contact details here. |

## [notify]

| Key | Default | Meaning |
|---|---|---|
| `default` | `["console"]` | Channels for job messages when the job has no `notify` of its own. |
| `on_failure` | `["console"]` | Channels for "job failed" and "job recovered" alerts. |
| `repeat_failure_alert_minutes` | `60` | While a job keeps failing, alert again at most this often. `0` alerts on every failed run. |

Channels: `console` (prints to standard output; always available), `slack`, `email`.

## [notify.email]

| Key | Default | Meaning |
|---|---|---|
| `host` | none | SMTP server. |
| `port` | `587` | `587` for STARTTLS, `465` for implicit TLS, `25` for a local relay. |
| `security` | `"starttls"` | `starttls`, `ssl` (implicit TLS) or `none` (only for a relay on the same machine or network). |
| `username` | none | Login name; the password comes from `SMTP_PASSWORD`. Leave it out for a relay without login. |
| `sender` | none | The From address, e.g. `"Automation <alerts@example.com>"`. |
| `recipients` | `[]` | To addresses. |
| `timeout_seconds` | `30` | SMTP connection timeout. |

## [llm]

Optional. Used by rss-digest when `summarize = true`. Off while `provider` is unset or its key is missing.

| Key | Default | Meaning |
|---|---|---|
| `provider` | none | `"anthropic"` or `"openai"`. |
| `model` | `claude-opus-5-5` / `gpt-6-luna` | Any model id the provider accepts. Defaults checked on 2026-10-01 against the providers' model pages. |
| `max_tokens` | `16000` | Output limit per request. |
| `effort` | `"low"` | Anthropic only: `output_config.effort`, one of `low`, `medium`, `high`, `xhigh`, `max`, or `none` to leave it out. `low` is enough for a short summary. |
| `fallbacks` | `true` | Anthropic only: server-side fallbacks (`fallbacks: "default"`, beta header `server-side-fallback-2026-07-01`), so a request the model declines is re-run on Anthropic's recommended fallback model. |
| `timeout_seconds` | `120` | Per request. |

For a model that does not accept effort or server-side fallbacks, set `effort = "none"` and `fallbacks = false`.
The request formats are described at the top of [llm.py](../src/automation/llm.py).

## [jobs.\<name\>]

One section per job. A job without a section is not scheduled (it may need settings, such as URLs). Every section
accepts these keys, plus the job's own:

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `true` | `false` turns the job off for the scheduler and for `automation run`. |
| `schedule` | the job's default | Cron expression, 5 fields: minute hour day-of-month month day-of-week, e.g. `"*/5 * * * *"`, `"0 7 * * mon-fri"`. |
| `notify` | `[notify] default` | Channels for this job's messages. |
| `max_runtime_minutes` | `60` | How long a run may hold the job's lock. After that, another run may start (see [reliability.md](reliability.md#overlapping-runs)). |

The example jobs' own keys:

| Job | Keys |
|---|---|
| uptime-check | `urls` (required), `expect_status` (default: any 200-399), `contains`, `timeout_seconds`, `retries` (default 1), `alert_on_first_check` (default true) |
| rss-digest | `feeds` (required), `max_items` (20), `title`, `first_run` (`"send"` or `"skip"`), `summarize` (false) |
| csv-report | `source` (path or URL, required), `group_by` (required), `sum` (columns to total), `title`, `delimiter` (`","`), `once_per` (`"day"`, `"week"`, `"month"` or `"run"`) |
| github-weekly | `repos` (`owner/name`, required), `days` (7), `title`, `send_empty` (false) |

## Overrides from the environment

Any setting outside `[jobs]` can be overridden with `AUTOMATION_<SECTION>__<KEY>` (two underscores between the
section and the key). Lists are written as JSON.

```bash
AUTOMATION_LOG__LEVEL=DEBUG
AUTOMATION_STATE__PATH=/var/lib/automation/automation.db
AUTOMATION_NOTIFY__ON_FAILURE='["slack","email"]'
AUTOMATION_TIMEZONE=Europe/Berlin
```

Empty values are ignored, so the empty lines in `.env.example` change nothing.
