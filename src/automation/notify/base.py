"""Notification messages, the notifier interface and the dispatcher jobs use as ``ctx.notify``."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import ClassVar, Literal

Level = Literal["info", "warning", "error"]

log = logging.getLogger("automation.notify")


@dataclass(frozen=True)
class Message:
    """What a job wants to tell people. ``text`` is plain text; ``html`` is an optional richer body for email."""

    title: str
    text: str
    level: Level = "info"
    html: str | None = None
    fields: dict[str, str] = field(default_factory=dict)
    link: str | None = None


class NotifyError(Exception):
    """A notification could not be delivered (after the HTTP client's retries)."""


class Notifier(ABC):
    name: ClassVar[str]

    @abstractmethod
    def configured(self) -> tuple[bool, str]:
        """(ready, description). The description never contains secrets."""

    @abstractmethod
    def send(self, message: Message, *, job: str | None = None, run_id: str | None = None) -> None:
        """Deliver the message or raise :class:`NotifyError`."""


@dataclass
class PlannedNotification:
    channel: str
    message: Message
    ready: bool
    note: str


class Dispatcher:
    """Sends a message to channels by name. In a dry run it records what it would send instead."""

    def __init__(
        self,
        notifiers: Mapping[str, Notifier],
        *,
        default: list[str],
        dry_run: bool = False,
        job: str | None = None,
        run_id: str | None = None,
    ) -> None:
        self.notifiers = notifiers
        self.default = list(default)
        self.dry_run = dry_run
        self.job = job
        self.run_id = run_id
        self.planned: list[PlannedNotification] = []
        self.sent: list[tuple[str, Message]] = []

    def send(self, message: Message, channels: list[str] | None = None) -> list[str]:
        """Send to ``channels`` (default: the job's channels). Tries every channel, then raises
        :class:`NotifyError` if any of them failed. Returns the channels that delivered."""
        targets = list(dict.fromkeys(channels or self.default))
        delivered: list[str] = []
        errors: list[str] = []
        for channel in targets:
            notifier = self.notifiers.get(channel)
            if notifier is None:
                errors.append(f"{channel}: unknown channel")
                continue
            ready, note = notifier.configured()
            if self.dry_run:
                self.planned.append(PlannedNotification(channel, message, ready, note))
                log.info("dry run: notification not sent", extra={"channel": channel, "title": message.title})
                continue
            if not ready:
                errors.append(f"{channel}: not configured ({note})")
                continue
            try:
                notifier.send(message, job=self.job, run_id=self.run_id)
            except Exception as exc:  # one broken channel must not stop the others
                errors.append(f"{channel}: {exc}")
                log.error(
                    "notification failed",
                    extra={"channel": channel, "title": message.title, "error": str(exc)},
                )
                continue
            delivered.append(channel)
            self.sent.append((channel, message))
            log.info("notification sent", extra={"channel": channel, "title": message.title})
        if errors:
            raise NotifyError("; ".join(errors))
        return delivered
