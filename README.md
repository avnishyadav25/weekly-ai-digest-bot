# Weekly AI digest bot

Once a week, reads the AI news feeds I follow and sends one digest of the posts I have not seen yet: the full
digest by email (grouped by source, with a two-or-three sentence summary per source from DeepSeek, or Claude/OpenAI) and a short version
in Slack. Posts are remembered in a local SQLite file, so nothing is sent twice. A feed that fails is named in the
digest and does not stop the others.

Built on my [Python Automation Boilerplate](https://avnishyadav.com/products/python-automation-boilerplate): the job is
`src/automation/jobs/weekly_digest.py`, its tests are `tests/test_job_weekly_digest.py`, and the settings are in
`config.toml`.

## Feeds

| Source | Feed |
|---|---|
| OpenAI News | https://openai.com/news/rss.xml |
| Google DeepMind | https://deepmind.google/blog/rss.xml |
| Google AI blog | https://blog.google/technology/ai/rss/ |
| Hugging Face blog | https://huggingface.co/blog/feed.xml |
| Salesforce Engineering | https://engineering.salesforce.com/feed/ |

Anthropic publishes no RSS feed, and developer.salesforce.com/blogs/feed refuses automated clients (HTTP 403), so
neither is in the list. Add or remove feeds in `config.toml`; `names` sets a display name when a feed's own title is
unhelpful.

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env        # then fill in EMAIL_USER, EMAIL_PASS, SLACK_WEBHOOK_URL, DEEPSEEK_API_KEY
```

Email goes through Gmail SMTP: `EMAIL_USER` is the Gmail address (also the sender and recipient unless
`[notify.email]` says otherwise) and `EMAIL_PASS` a Gmail app password. Summaries use `[llm] provider = "deepseek"`
(`DEEPSEEK_API_KEY`); `anthropic` and `openai` also work. Secrets only ever go in `.env`, which git ignores.

```bash
export PYTHONPATH=src                                   # see "Python 3.14 on Desktop" below
.venv/bin/python -m automation doctor                    # checks config, state path and channels
.venv/bin/python -m automation run weekly-ai-digest --dry-run   # prints the digest; sends and saves nothing
.venv/bin/python -m automation run weekly-ai-digest      # sends it for real and remembers the posts
.venv/bin/python -m pytest -q                            # tests, no network
```

Without the LLM key the digest still goes out, without summaries. If the Slack message fails (no webhook, or
Slack is down), the email still counts as sent, the run summary says "Slack summary failed", and nothing is sent twice.
Set `slack_channels = []` to send email only.

## Every Monday at 07:00 IST (launchd)

```bash
mkdir -p state
cp deploy/launchd/com.avnishyadav.weekly-ai-digest.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.avnishyadav.weekly-ai-digest.plist
```

The agent checks every 15 minutes whether the digest is due. If the Mac is asleep at 07:00 on Monday, the digest goes
out at the first check after it wakes. Logs: `state/launchd-stdout.txt` and `state/launchd-stderr.txt`. Stop it with
`launchctl bootout gui/$(id -u)/com.avnishyadav.weekly-ai-digest`.

## Python 3.14 on Desktop

macOS sets the "hidden" flag on some files under `~/Desktop` (for example when Desktop syncs to iCloud), and Python 3.14
skips hidden `.pth` files, which breaks the editable install. The tests (`pythonpath` in `pyproject.toml`) and the
launchd agent (`PYTHONPATH=src`) therefore import from `src` directly. In a shell, `export PYTHONPATH=src` does the same.

## Sample output

[docs/sample-digest.md](docs/sample-digest.md) is a real dry run against the five feeds (no email or Slack message
sent, no summaries because no API key was set).

## Licence

Published free by the author. The automation framework under `src/automation` comes from the
[Python Automation Boilerplate](https://avnishyadav.com/products/python-automation-boilerplate). TODO(owner): choose a
licence for this repository.
