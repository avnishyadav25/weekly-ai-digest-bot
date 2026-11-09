# Scheduling

Each job's schedule is a cron expression in its config section (or the job's default). There are two ways to act
on those schedules, and five ready-made recipes in `deploy/`.

## Two ways to run on a schedule

**In-process: `automation schedule`.** One long-running process. It reads every enabled job that has a section and
a schedule, and fires each one on time in a small thread pool (APScheduler 3). Use it where something keeps a
process alive and restarts it: Docker, or a systemd service.

**External tick: `automation schedule --once`.** Something else (cron, a systemd timer, launchd, GitHub Actions)
starts the command every minute. It runs each job that is due and exits. A job is due when one of its scheduled
times has passed since its previous run, or when it has never run. Nothing has to stay running, and a machine that
was asleep catches up with one run per job when it wakes.

You can also skip the config schedules and let the outside scheduler call one job directly:
`automation run uptime-check`. Leave `schedule` out of that job's section so `schedule --once` does not run it too.

| | In-process | External tick |
|---|---|---|
| Command | `automation schedule` | `automation schedule --once` (or `automation run <job>`) |
| Precision | to the second | the tick interval (run it every minute) |
| Process | always running, uses some memory | starts, runs what is due, exits |
| Restarts and crashes | needs a supervisor (Docker `restart`, systemd `Restart=`) | nothing to restart |
| Missed runs while down | skipped; each job runs at its next scheduled time | one catch-up run per job on the next tick |
| Recipes | Docker, systemd service | systemd timer, cron, launchd, GitHub Actions |

## The recipes

| Platform | Files | Runs |
|---|---|---|
| Docker / Docker Compose | [deploy/docker/Dockerfile](../deploy/docker/Dockerfile), [docker-compose.yml](../docker-compose.yml) | `automation schedule` in a container, as a non-root user, state in a named volume |
| systemd (Linux) | [deploy/systemd/automation.service](../deploy/systemd/automation.service), [deploy/systemd/automation.timer](../deploy/systemd/automation.timer) | `automation schedule --once` every minute |
| launchd (macOS) | [deploy/launchd/com.example.automation.plist](../deploy/launchd/com.example.automation.plist) | `automation schedule --once` every 60 seconds while you are logged in |
| cron | [deploy/cron/crontab.example](../deploy/cron/crontab.example) | `automation schedule --once` every minute, or single jobs |
| GitHub Actions | [deploy/github-actions/scheduled-job.yml](../deploy/github-actions/scheduled-job.yml) | `automation run uptime-check` every 15 minutes, secrets by name, state in the Actions cache |

Step-by-step setup for each one is in [deployment.md](deployment.md).

To run the in-process scheduler under systemd instead of the timer, change `automation.service` to
`Type=simple`, `ExecStart=/opt/automation/.venv/bin/automation schedule`, add `Restart=on-failure` and
`TimeoutStopSec=2min`, give it an `[Install]` section with `WantedBy=multi-user.target`, and do not install the
timer.

## Which one to pick

- A server you control: the systemd timer. Nothing stays running, logs go to the journal, and a tick never
  overlaps the previous one.
- Already running containers: Docker Compose with the in-process scheduler.
- Your Mac: the launchd agent (it runs only while you are logged in and the Mac is awake).
- No server at all: GitHub Actions. Schedules there run in UTC, at most every 5 minutes, can start late when Actions
  is busy (the start of each hour is the busiest), and stop after 60 days without activity in public repositories.
  Good for daily digests and reports; too coarse for tight uptime checks.
- cron when that is what the machine already uses.

## Cron expressions

Five fields: minute, hour, day of month, month, day of week. Day names (`mon-fri`) and steps (`*/15`) work.

| Expression | When |
|---|---|
| `*/5 * * * *` | every 5 minutes |
| `0 7 * * *` | 07:00 every day |
| `0 9 * * mon` | 09:00 on Mondays |
| `30 8 1 * *` | 08:30 on the first of the month |
| `0 */6 * * mon-fri` | every 6 hours on weekdays |

The schedules use `timezone` from the config (default UTC). With a local time zone, a job scheduled in the hour
that daylight saving time skips or repeats behaves differently on the change days; keep such jobs outside
01:00-03:00 local time, or schedule in UTC.

## Overlap

A job never runs twice at the same time:

- `automation schedule` uses APScheduler's `max_instances = 1`: if a run is still going when the next time
  arrives, that time is skipped and a warning is logged.
- Every run, however it was started, takes the job's lock in the state database first. A second process (a cron
  tick, a manual `automation run`) that finds the lock held skips the run. See
  [reliability.md](reliability.md#overlapping-runs).
- The systemd timer, launchd and the GitHub Actions `concurrency` group also never start a second copy of the
  tick while one is running.

## Stopping

`automation schedule` stops on Ctrl+C or SIGTERM (`docker stop`, `systemctl stop`): it stops firing new runs and
waits for the running ones to finish. Give it enough stop time for your longest job (`stop_grace_period` in
`docker-compose.yml`, `TimeoutStopSec` in systemd).
