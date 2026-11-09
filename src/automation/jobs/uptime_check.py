"""uptime-check: request each URL, decide up or down, and alert only when a URL changes state."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime

import httpx
from pydantic import Field

from automation.jobs.base import Context, Job, JobConfig, Result
from automation.notify import Message


class UptimeConfig(JobConfig):
    urls: list[str] = Field(min_length=1, description="URLs to check")
    expect_status: list[int] = Field(
        default_factory=list, description="accepted statuses; empty means 200-399"
    )
    contains: str | None = Field(default=None, description="text the body must contain")
    timeout_seconds: float | None = Field(default=None, gt=0, description="per-check timeout")
    retries: int = Field(default=1, ge=0, le=5, description="quick retries before calling a URL down")
    alert_on_first_check: bool = Field(
        default=True, description="alert when a URL is down on its first check"
    )


@dataclass
class Check:
    url: str
    up: bool
    detail: str
    status: int | None = None
    ms: int | None = None


def _ago(since: str | None, now: datetime) -> str:
    if not since:
        return ""
    minutes = int((now - datetime.fromisoformat(since)).total_seconds() // 60)
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60} min"


class UptimeCheck(Job[UptimeConfig]):
    name = "uptime-check"
    description = "Checks URLs and alerts when one goes down or comes back."
    schedule = "*/5 * * * *"
    config_model = UptimeConfig

    def check(self, ctx: Context[UptimeConfig], url: str) -> Check:
        cfg = ctx.config
        kwargs = {"timeout": cfg.timeout_seconds} if cfg.timeout_seconds else {}
        started = time.monotonic()
        try:
            response = ctx.http.get(url, retries=cfg.retries, raise_for_status=False, **kwargs)
        except httpx.TransportError as exc:
            return Check(url, False, f"{type(exc).__name__}: {exc}".rstrip(": "))
        ms = int((time.monotonic() - started) * 1000)
        status = response.status_code
        ok_status = status in cfg.expect_status if cfg.expect_status else 200 <= status < 400
        if not ok_status:
            return Check(url, False, f"HTTP {status}", status, ms)
        if cfg.contains and cfg.contains not in response.text:
            return Check(url, False, f"HTTP {status} but the text {cfg.contains!r} is missing", status, ms)
        return Check(url, True, f"HTTP {status}", status, ms)

    def run(self, ctx: Context[UptimeConfig]) -> Result:
        checks = [self.check(ctx, url) for url in ctx.config.urls]
        down_lines: list[str] = []
        up_lines: list[str] = []
        new_states: dict[str, dict[str, str]] = {}
        for check in checks:
            key = f"status:{check.url}"
            previous = ctx.state.get(key)
            state = "up" if check.up else "down"
            timing = f" in {check.ms} ms" if check.ms is not None else ""
            ctx.log.info(
                "checked", extra={"url": check.url, "state": state, "detail": check.detail, "ms": check.ms}
            )
            if previous is None or previous.get("state") != state:
                since = ctx.now.isoformat()
                if state == "down" and (previous is not None or ctx.config.alert_on_first_check):
                    down_lines.append(f"{check.url} is down: {check.detail}{timing}")
                elif state == "up" and previous is not None:
                    up_lines.append(
                        f"{check.url} is back up: {check.detail}{timing} "
                        f"(down for {_ago(previous.get('since'), ctx.now)})"
                    )
            else:
                since = previous.get("since", ctx.now.isoformat())
            new_states[key] = {"state": state, "since": since, "detail": check.detail}

        if down_lines or up_lines:
            down = len(down_lines)
            title = (
                f"{down} URL{'s' if down != 1 else ''} down"
                if down
                else f"{len(up_lines)} URL{'s' if len(up_lines) != 1 else ''} back up"
            )
            if down and up_lines:
                title += f", {len(up_lines)} back up"
            ctx.notify.send(
                Message(
                    title=f"uptime-check: {title}",
                    text="\n".join(down_lines + up_lines),
                    level="error" if down else "info",
                )
            )
        # Save after notifying: if the alert fails, the next run sees the same change and alerts again.
        for key, value in new_states.items():
            ctx.state.set(key, value)

        down_now = sum(1 for c in checks if not c.up)
        changes = len(down_lines) + len(up_lines)
        return Result(
            summary=f"{len(checks)} checked: {len(checks) - down_now} up, {down_now} down; "
            f"{changes} change{'s' if changes != 1 else ''} alerted",
            data={"checked": len(checks), "down": down_now, "alerted": changes},
        )
