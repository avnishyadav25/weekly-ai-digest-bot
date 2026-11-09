"""rss-digest: read feeds, skip items already sent, send one digest of the new ones."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Literal

from pydantic import Field

from automation.integrations.rss import FeedItem, fetch_feed
from automation.jobs.base import Context, Job, JobConfig, Result
from automation.llm import LlmError
from automation.notify import Message

_OLDEST = datetime.min.replace(tzinfo=UTC)


class RssDigestConfig(JobConfig):
    feeds: list[str] = Field(min_length=1, description="RSS or Atom feed URLs")
    max_items: int = Field(default=20, gt=0, description="items listed in one digest; the rest are counted")
    title: str = "New posts"
    first_run: Literal["send", "skip"] = Field(
        default="send", description="'skip' marks existing items as seen on the first run without sending"
    )
    summarize: bool = Field(default=False, description="add a summary from [llm] when it is configured")


class RssDigest(Job[RssDigestConfig]):
    name = "rss-digest"
    description = "Reads RSS/Atom feeds and sends a digest of items not sent before."
    schedule = "0 7 * * *"
    config_model = RssDigestConfig

    def run(self, ctx: Context[RssDigestConfig]) -> Result:
        cfg = ctx.config
        first_run = ctx.state.count_seen() == 0
        entries: dict[str, tuple[str, FeedItem]] = {}
        for url in cfg.feeds:
            feed = fetch_feed(ctx.http, url)
            ctx.log.info("feed read", extra={"feed": url, "items": len(feed.items)})
            for item in feed.items:
                entries.setdefault(f"{url} {item.id}", (feed.title, item))

        new_keys = ctx.state.unseen(entries)
        if not new_keys:
            return Result(summary="no new items", data={"new": 0})
        if first_run and cfg.first_run == "skip":
            ctx.state.mark_seen(new_keys)
            return Result(
                summary=f"first run: marked {len(new_keys)} existing items as seen", data={"new": 0}
            )

        new = sorted((entries[k] for k in new_keys), key=lambda e: e[1].published or _OLDEST, reverse=True)
        shown, rest = new[: cfg.max_items], len(new) - cfg.max_items
        lines = [
            f"- {item.title} ({source})" + (f"\n  {item.link}" if item.link else "") for source, item in shown
        ]
        if rest > 0:
            lines.append(f"... and {rest} more")
        text = "\n".join(lines)
        html_items = "".join(
            f'<li><a href="{html.escape(item.link or "#")}">{html.escape(item.title)}</a> '
            f"<small>{html.escape(source)}</small></li>"
            for source, item in shown
        )
        summary = self.summary(ctx, text)
        if summary:
            text = f"{summary}\n\n{text}"
        body_html = (f"<p>{html.escape(summary)}</p>" if summary else "") + f"<ul>{html_items}</ul>"
        if rest > 0:
            body_html += f"<p>... and {rest} more</p>"

        ctx.notify.send(Message(title=f"{cfg.title}: {len(new)} new", text=text, html=body_html))
        ctx.state.mark_seen(new_keys)  # only after the digest went out
        return Result(
            summary=f"{len(new)} new item{'s' if len(new) != 1 else ''} sent", data={"new": len(new)}
        )

    def summary(self, ctx: Context[RssDigestConfig], text: str) -> str | None:
        if not ctx.config.summarize:
            return None
        if ctx.llm is None:
            ctx.log.warning("summarize is on but [llm] is not configured; sending without a summary")
            return None
        try:
            return ctx.llm.summarize(text)
        except LlmError as exc:  # a summary is a nice-to-have; the digest still goes out
            ctx.log.warning("summary failed; sending without it", extra={"error": str(exc)})
            return None
