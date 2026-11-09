"""github-weekly: pull requests merged in the last N days in the configured repositories, as one digest.

Each pull request is listed once: it is marked as seen after the digest goes out, so running the job daily with
``days = 7`` sends only what is new since the previous digest.
"""

from __future__ import annotations

import html
from datetime import timedelta

from pydantic import Field, field_validator

from automation.integrations.github import GitHub, GitHubError
from automation.jobs.base import Context, Job, JobConfig, JobFailed, Result
from automation.notify import Message


class GithubWeeklyConfig(JobConfig):
    repos: list[str] = Field(min_length=1, description="repositories as owner/name")
    days: int = Field(default=7, gt=0, le=90)
    title: str = "Merged pull requests"
    send_empty: bool = Field(default=False, description="send a message even when nothing was merged")

    @field_validator("repos")
    @classmethod
    def _owner_name(cls, repos: list[str]) -> list[str]:
        bad = [r for r in repos if r.count("/") != 1 or r.startswith("/") or r.endswith("/")]
        if bad:
            raise ValueError(f"use owner/name, not {', '.join(bad)}")
        return repos


class GithubWeekly(Job[GithubWeeklyConfig]):
    name = "github-weekly"
    description = "Digest of pull requests merged in the last 7 days in your repositories."
    schedule = "0 9 * * 1"
    config_model = GithubWeeklyConfig

    def run(self, ctx: Context[GithubWeeklyConfig]) -> Result:
        cfg = ctx.config
        since = ctx.now - timedelta(days=cfg.days)
        github = GitHub(ctx.http, ctx.secrets.github_token)
        found = []
        for repo in cfg.repos:
            try:
                prs = github.merged_pull_requests(repo, since)
            except GitHubError as exc:
                raise JobFailed(f"{repo}: {exc}") from exc
            ctx.log.info("pull requests read", extra={"repo": repo, "merged": len(prs)})
            found.extend(prs)

        keys = {f"{pr.repo}#{pr.number}": pr for pr in found}
        new_keys = ctx.state.unseen(keys)
        new = [keys[k] for k in new_keys]
        if not new and not cfg.send_empty:
            return Result(summary="no newly merged pull requests", data={"merged": 0})

        lines: list[str] = []
        parts_html: list[str] = []
        for repo in cfg.repos:
            prs = [pr for pr in new if pr.repo == repo]
            if not prs:
                continue
            lines.append(f"{repo} ({len(prs)})")
            lines += [
                f"- #{pr.number} {pr.title} by {pr.author}, {pr.merged_at:%a %d %b}\n  {pr.url}" for pr in prs
            ]
            items = "".join(
                f'<li><a href="{html.escape(pr.url)}">#{pr.number} {html.escape(pr.title)}</a> '
                f"by {html.escape(pr.author)}, {pr.merged_at:%a %d %b}</li>"
                for pr in prs
            )
            parts_html.append(f"<h3>{html.escape(repo)} ({len(prs)})</h3><ul>{items}</ul>")
        text = "\n".join(lines) if lines else f"Nothing merged in the last {cfg.days} days."
        window = f"{since:%d %b} to {ctx.now:%d %b %Y}"
        ctx.notify.send(
            Message(
                title=f"{cfg.title}: {len(new)} in {len(cfg.repos)} repo{'s' if len(cfg.repos) != 1 else ''}",
                text=f"{window}\n\n{text}",
                html=f"<p>{window}</p>" + ("".join(parts_html) or f"<p>{html.escape(text)}</p>"),
            )
        )
        ctx.state.mark_seen(new_keys)  # only after the digest went out
        return Result(
            summary=f"{len(new)} merged pull request{'s' if len(new) != 1 else ''} sent",
            data={"merged": len(new)},
        )
