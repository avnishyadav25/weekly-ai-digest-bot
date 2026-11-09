# Getting started

From an unzipped copy to a job that alerts you, in five steps. Every command runs from the project folder (the
folder that holds `pyproject.toml`).

## 1. Install

You need Python 3.11 or newer (`python3 --version`).

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
python -m pip install -r requirements.txt -r requirements-dev.txt
```

`make install` does the same. `requirements.txt` pins the runtime packages exactly; `requirements-dev.txt` adds the
project itself (editable, so the `automation` command runs your working copy), pytest, respx and ruff.

Check it:

```bash
automation --help
python -m pytest -q
```

## 2. A first run, offline and without side effects

```bash
automation run uptime-check --dry-run --offline --config examples/config.example.toml
```

- `--offline` answers the job's HTTP requests from `examples/fixtures/` (one URL returns 200, one returns 503),
  so nothing leaves your machine.
- `--dry-run` runs the job end to end but sends no notification and saves no state. It prints what it would have
  sent and saved.

The JSON lines are the logs (on stderr, one object per event, each with the job name and run id). The block after
them is the plan: one alert for the URL that is down, and the state the job would remember so it does not alert
twice.

Two more commands worth running now:

```bash
automation jobs list --config examples/config.example.toml
automation doctor --config examples/config.example.toml
```

`jobs list` shows every job, whether it is configured, and its schedule. `doctor` checks the config, the state
folder, and which notification channels are set up.

## 3. Your own config

```bash
cp examples/config.example.toml config.toml
cp .env.example .env
```

`config.toml` holds settings (no secrets; commit it). `.env` holds secrets (never commit it; `.gitignore` already
excludes it). Without `--config`, the commands read `config.toml` in the current folder. Every setting is listed in
[configuration.md](configuration.md).

## 4. The example jobs

Four jobs ship in `src/automation/jobs/`. Each has a section in `examples/config.example.toml`, a fixture for
`--offline`, and tests. Use them as they are or as starting points for your own.

- **uptime-check** requests each URL in `urls` and decides whether it is up (status 200-399 by default, or the
  statuses in `expect_status`, and optionally a `contains` text). It alerts only when a URL changes state: one
  message when it goes down, one when it is back up with how long it was down. A quick retry (`retries`) avoids
  alerts for a single failed request.
- **rss-digest** reads the RSS or Atom feeds in `feeds`, skips the items it already sent, and sends one digest of
  the new ones (newest first, at most `max_items`). Items are marked as sent only after the digest goes out. With
  `first_run = "skip"` the first run only remembers the existing items. With `summarize = true` and an `[llm]`
  provider, the digest starts with a short summary.
- **csv-report** reads a CSV file (a path or a URL), groups the rows by `group_by`, totals the `sum` columns and
  sends a table as plain text and HTML (send it by email with `notify = ["email"]`). `once_per = "week"` sends at
  most one report per ISO week, so a re-run does not send a duplicate.
- **github-weekly** lists the pull requests merged in the last `days` days (7 by default) in the repositories in
  `repos`, grouped by repository. Each pull request is reported once. Public repositories work without a token;
  set `GITHUB_TOKEN` for private ones and a higher rate limit.

## 5. Alerts on Slack or email

**Slack.** Create an incoming webhook in your workspace (in a Slack app: Incoming Webhooks, then Add New Webhook,
and pick the channel). Put the URL in `.env`:

```bash
SLACK_WEBHOOK_URL=<the webhook URL Slack shows you>
```

Then send alerts there, in `config.toml`:

```toml
[notify]
on_failure = ["slack", "console"]   # "job failed" alerts

[jobs.uptime-check]
notify = ["slack"]                   # this job's own messages
```

Try it with the fixtures while `config.toml` still has the example URLs (in the fixtures, one of them is down):

```bash
automation run uptime-check --offline    # sends one Slack alert: 1 URL down
automation run uptime-check --offline    # nothing new: no second alert
```

`--offline` replaces only the job's HTTP responses; notifications still go out unless you add `--dry-run`. To
start over, forget what the job remembered: `automation state clear --job uptime-check --yes`.

**Email.** Fill in `[notify.email]` (host, port, security, username, sender, recipients) and put the password in
`.env` as `SMTP_PASSWORD`. Use `"email"` in `notify` or `on_failure`. `automation doctor` tells you what is missing.

## Next

- Run jobs on a schedule: [scheduling.md](scheduling.md), then [deployment.md](deployment.md).
- Write your own job: [writing-a-job.md](writing-a-job.md).
- What happens when things fail: [reliability.md](reliability.md).
