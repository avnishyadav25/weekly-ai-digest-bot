"""weekly-ai-digest: the AI news I follow, once a week, grouped by source with a short summary per source.

The full digest (every new item, grouped, with summaries) goes to ``email_channels``; a short version (one summary
line and the item count per source) goes to ``slack_channels``. Items are remembered in SQLite, so a re-run never
repeats them; a feed that fails is reported in the digest and does not stop the others.
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta

from pydantic import Field

from automation.integrations.rss import FeedError, FeedItem, fetch_feed
from automation.jobs.base import Context, Job, JobConfig, Result
from automation.llm import LlmError
from automation.notify import Message, NotifyError

_OLDEST = datetime.min.replace(tzinfo=UTC)
SOURCE_PROMPT = (
    "These are this week's new posts from {source}. In two or three sentences, say what they announce or cover. "
    "Use only the titles and descriptions given; do not add facts, numbers or opinions. "
    "Answer in plain sentences, not a list."
)
_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+")


def title_key(title: str) -> str:
    """Case, punctuation and spacing don't make two announcements different."""
    return " ".join(re.sub(r"[^\w\s]", " ", title.casefold()).split())


def as_prose(text: str) -> str:
    """A summary that came back as a list becomes one paragraph (Slack shows only the first line)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not any(_BULLET.match(ln) for ln in lines):
        return text.strip()
    parts = [_BULLET.sub("", ln).strip() for ln in lines]
    return " ".join(p if p[-1] in ".!?" else p + "." for p in parts if p)


def merge_duplicates(groups: dict[str, list[FeedItem]]) -> dict[str, list[str]]:
    """Keep each title once, under the first source (config order); returns title key -> the other sources.

    Groups left empty are removed. The dropped items stay in the run's keys, so they are still marked sent.
    """
    first: dict[str, str] = {}
    also: dict[str, list[str]] = {}
    for source in list(groups):
        kept = []
        for item in groups[source]:
            key = title_key(item.title)
            if key in first and first[key] != source:
                also.setdefault(key, []).append(source)
            else:
                first.setdefault(key, source)
                kept.append(item)
        if kept:
            groups[source] = kept
        else:
            del groups[source]
    return also


class WeeklyDigestConfig(JobConfig):
    feeds: list[str] = Field(min_length=1, description="RSS or Atom feed URLs")
    lookback_days: int = Field(
        default=8, gt=0, description="ignore items published before this many days ago"
    )
    max_per_source: int = Field(default=8, gt=0, description="items listed per source; the rest are counted")
    summarize: bool = Field(
        default=True, description="a short summary per source from [llm], when configured"
    )
    email_channels: list[str] = Field(default_factory=lambda: ["email"], description="get the full digest")
    slack_channels: list[str] = Field(default_factory=lambda: ["slack"], description="get the short version")
    names: dict[str, str] = Field(
        default_factory=dict, description="feed URL -> display name (default: the feed's own title)"
    )
    title: str = "Weekly AI digest"


class WeeklyDigest(Job[WeeklyDigestConfig]):
    name = "weekly-ai-digest"
    description = "Weekly digest of new posts from AI vendor blogs, grouped by source, by email and Slack."
    schedule = "0 7 * * 1"  # Mondays 07:00 in [timezone]
    config_model = WeeklyDigestConfig

    def run(self, ctx: Context[WeeklyDigestConfig]) -> Result:
        cfg = ctx.config
        since = ctx.now - timedelta(days=cfg.lookback_days)
        groups: dict[str, list[FeedItem]] = {}
        keys: list[str] = []
        failed: list[str] = []
        for url in cfg.feeds:
            try:
                feed = fetch_feed(ctx.http, url)
            except FeedError as exc:  # one broken feed must not cost the whole digest
                ctx.log.warning("feed failed", extra={"feed": url, "error": str(exc)})
                failed.append(url)
                continue
            # undated items count as new; dated ones only inside the look-back window
            recent = {f"{url} {i.id}": i for i in feed.items if i.published is None or i.published >= since}
            new = ctx.state.unseen(recent)
            ctx.log.info("feed read", extra={"feed": url, "items": len(feed.items), "new": len(new)})
            if new:
                items = sorted((recent[k] for k in new), key=lambda i: i.published or _OLDEST, reverse=True)
                groups.setdefault(cfg.names.get(url) or feed.title or url, []).extend(items)
                keys.extend(new)

        also = merge_duplicates(groups)
        if not groups:
            note = f"; {len(failed)} feed(s) failed" if failed else ""
            return Result(summary=f"no new items{note}", data={"new": 0, "failed": failed})

        summaries = {source: self.summary(ctx, source, items) for source, items in groups.items()}
        total = sum(len(i) for i in groups.values())
        title = f"{cfg.title}: {total} new from {len(groups)} source{'s' if len(groups) != 1 else ''}"

        ctx.notify.send(
            Message(
                title=title,
                text=self.full_text(cfg, groups, summaries, failed, also),
                html=self.full_html(cfg, groups, summaries, failed, also),
            ),
            channels=cfg.email_channels,
        )
        # the email is the digest: once it is out the posts count as sent, so a Slack failure never re-sends it
        ctx.state.mark_seen(keys)
        note = ""
        if cfg.slack_channels:
            try:
                ctx.notify.send(
                    Message(title=title, text=self.short_text(groups, summaries)), channels=cfg.slack_channels
                )
            except NotifyError as exc:
                ctx.log.warning("slack summary failed", extra={"error": str(exc)})
                note = "; Slack summary failed"
        return Result(
            summary=f"{total} new item{'s' if total != 1 else ''} sent{note}",
            data={"new": total, "failed": failed},
        )

    def summary(self, ctx: Context[WeeklyDigestConfig], source: str, items: list[FeedItem]) -> str | None:
        if not ctx.config.summarize:
            return None
        if ctx.llm is None:
            ctx.log.warning("summarize is on but [llm] is not configured; sending without summaries")
            return None
        listing = "\n".join(f"- {i.title}: {i.summary}" for i in items[: ctx.config.max_per_source])
        try:
            return as_prose(ctx.llm.summarize(listing, instruction=SOURCE_PROMPT.format(source=source)))
        except LlmError as exc:  # a summary is a nice-to-have; the digest still goes out
            ctx.log.warning("summary failed", extra={"source": source, "error": str(exc)})
            return None

    @staticmethod
    def full_text(cfg, groups, summaries, failed, also=None) -> str:
        also = also or {}

        def tail(i):
            others = also.get(title_key(i.title))
            return f" (also on {', '.join(others)})" if others else ""

        out = []
        for source, items in groups.items():
            out.append(f"## {source} ({len(items)} new)")
            if summaries.get(source):
                out.append(summaries[source])
            out += [
                f"- {i.title}{tail(i)}" + (f"\n  {i.link}" if i.link else "")
                for i in items[: cfg.max_per_source]
            ]
            if len(items) > cfg.max_per_source:
                out.append(f"... and {len(items) - cfg.max_per_source} more")
            out.append("")
        if failed:
            out.append("Could not read: " + ", ".join(failed))
        return "\n".join(out).strip()

    @staticmethod
    def full_html(cfg, groups, summaries, failed, also=None) -> str:
        also = also or {}
        e = html.escape
        parts = []
        for source, items in groups.items():
            parts.append(f"<h2>{e(source)} <small>({len(items)} new)</small></h2>")
            if summaries.get(source):
                parts.append(f"<p>{e(summaries[source]).replace(chr(10), '<br>')}</p>")
            lis = "".join(
                f'<li><a href="{e(i.link or "#")}">{e(i.title)}</a>'
                + (
                    f" <small>(also on {e(', '.join(also[title_key(i.title)]))})</small>"
                    if title_key(i.title) in also
                    else ""
                )
                + "</li>"
                for i in items[: cfg.max_per_source]
            )
            parts.append(f"<ul>{lis}</ul>")
            if len(items) > cfg.max_per_source:
                parts.append(f"<p>... and {len(items) - cfg.max_per_source} more</p>")
        if failed:
            parts.append(f"<p><small>Could not read: {e(', '.join(failed))}</small></p>")
        return "".join(parts)

    @staticmethod
    def short_text(groups, summaries) -> str:
        lines = []
        for source, items in groups.items():
            first = (summaries.get(source) or items[0].title).splitlines()[0].lstrip("- ")
            lines.append(f"• {source} ({len(items)}): {first}")
        return "\n".join(lines) + "\nFull digest by email."
