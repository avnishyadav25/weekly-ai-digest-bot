# Deployment

How to install the project on a machine that runs your jobs, for each recipe in `deploy/`. Pick one with
[scheduling.md](scheduling.md). The steps assume Linux paths (`/opt/automation`) unless the section says otherwise.

## Before any of them

1. Copy the project to the machine (git clone of your copy, or the unzipped folder).
2. Create the virtualenv and install the pinned runtime packages and the project:

   ```bash
   cd /opt/automation
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m pip install --no-deps .
   ```

   (`requirements-dev.txt` is only needed where you run the tests.)
3. Create `config.toml` and `.env`, and protect `.env`:

   ```bash
   cp examples/config.example.toml config.toml   # edit it
   cp .env.example .env                           # fill in the secrets you use
   chmod 600 .env
   ```

4. Check it, then run each job once by hand:

   ```bash
   .venv/bin/automation doctor
   .venv/bin/automation run uptime-check --dry-run
   .venv/bin/automation run uptime-check
   ```

## systemd timer (Linux)

Runs `automation schedule --once` at the top of every minute as a dedicated user.

```bash
sudo useradd --system --home-dir /opt/automation --shell /usr/sbin/nologin automation
sudo mkdir -p /opt/automation/state
sudo chown -R automation:automation /opt/automation
sudo cp deploy/systemd/automation.service deploy/systemd/automation.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now automation.timer
systemctl list-timers automation.timer        # next tick
journalctl -u automation.service -f           # the JSON log lines
```

The service is read-only except `/opt/automation/state` (`ProtectSystem=strict`), so keep the state path inside
that folder. If your project lives elsewhere, change `WorkingDirectory`, `ExecStart` and `ReadWritePaths`.

## Docker Compose

Runs the in-process scheduler in a container built from [deploy/docker/Dockerfile](../deploy/docker/Dockerfile).

```bash
cp examples/config.example.toml config.toml
cp .env.example .env
docker compose up -d --build
docker compose logs -f automation
docker compose exec automation automation state show
docker compose exec automation automation run uptime-check --dry-run
```

The optional `env_file` entry needs Docker Compose 2.24 or newer. `config.toml` is mounted read-only; secrets come
from `.env` through `env_file` (they are not baked into the image:
`.dockerignore` keeps `.env` out of the build). State lives in the named volume `automation-state`. After a code or
config change: `docker compose up -d --build`. `docker compose stop` sends SIGTERM and waits up to
`stop_grace_period` (2 minutes) for running jobs.

## cron

```bash
crontab -e
```

Paste the line from [deploy/cron/crontab.example](../deploy/cron/crontab.example) and change the folder. cron
starts commands with a minimal environment, which is why the line uses `cd` and the full path to
`.venv/bin/automation`; the app reads `.env` from that folder. Output goes to `state/cron.txt`; rotate or trim it
from time to time.

## launchd (macOS)

1. Edit [deploy/launchd/com.example.automation.plist](../deploy/launchd/com.example.automation.plist): replace
   `/Users/you/automation` with your project folder in all four places.
2. Install and start it:

   ```bash
   mkdir -p state                                # launchd writes its two log files here
   cp deploy/launchd/com.example.automation.plist ~/Library/LaunchAgents/
   launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.automation.plist
   launchctl print gui/$(id -u)/com.example.automation | head -20
   ```

3. Stop it with `launchctl bootout gui/$(id -u)/com.example.automation`.

It runs while you are logged in and the Mac is awake; after sleep, the next tick catches up. Logs go to
`state/launchd-stdout.txt` and `state/launchd-stderr.txt`.

## GitHub Actions

1. Copy [deploy/github-actions/scheduled-job.yml](../deploy/github-actions/scheduled-job.yml) to
   `.github/workflows/scheduled-job.yml` in your repository and change the job name and cron if you want.
2. Commit `config.toml` (it holds no secrets).
3. Add the secrets it uses under Settings, Secrets and variables, Actions: `SLACK_WEBHOOK_URL`, `SMTP_PASSWORD`,
   and `GH_PAT` for private repositories (GitHub does not allow secret names that start with `GITHUB_`; the
   workflow passes it to the app as `GITHUB_TOKEN`).
4. Run it once by hand from the Actions tab (`workflow_dispatch`) and check the log.

The runner's disk is thrown away after each run, so the workflow restores the `state/` folder from the Actions
cache before the job and saves it afterwards, even when the job failed. GitHub removes cache entries that are not
used for 7 days, so a job that runs less often than weekly starts with empty state each time (uptime-check would
then not know the previous state of a URL). The actions are pinned to commit SHAs, with the version in a comment.

## Upgrading

```bash
cd /opt/automation
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip install --no-deps .
.venv/bin/automation doctor
```

Read `CHANGELOG.md` first; it lists any change that needs action, including changes to the state database. The
database creates its tables when they are missing.

## Security checklist

- `.env` readable only by the service user (`chmod 600`), never committed, never copied into an image.
- The service runs as its own user, not root (systemd recipe, Docker image).
- Use a Slack webhook for a channel meant for alerts, and a GitHub token with read-only access to the repositories
  the job reads.
- `automation doctor` before you rely on it, and again after changing the config.
