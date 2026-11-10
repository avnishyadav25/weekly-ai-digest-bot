"""Notification channels: console, Slack and email."""

from __future__ import annotations

from typing import TextIO

from automation.config import Secrets, Settings
from automation.http import HttpClient
from automation.notify.base import Dispatcher, Message, Notifier, NotifyError, PlannedNotification
from automation.notify.console import ConsoleNotifier
from automation.notify.email import EmailNotifier, SmtpFactory
from automation.notify.slack import SlackNotifier

__all__ = [
    "ConsoleNotifier",
    "Dispatcher",
    "EmailNotifier",
    "Message",
    "Notifier",
    "NotifyError",
    "PlannedNotification",
    "SlackNotifier",
    "build_notifiers",
]


def build_notifiers(
    settings: Settings,
    secrets: Secrets,
    http: HttpClient,
    *,
    console_stream: TextIO | None = None,
    smtp_factory: SmtpFactory | None = None,
) -> dict[str, Notifier]:
    """Every channel, configured or not; ``Notifier.configured()`` says which ones can send."""
    email = settings.notify.email
    if secrets.email_user:  # EMAIL_USER (Gmail style) fills whatever [notify.email] leaves empty
        email = email.model_copy(
            update={
                "username": email.username or secrets.email_user,
                "sender": email.sender or secrets.email_user,
                "recipients": email.recipients or [secrets.email_user],
            }
        )
    return {
        "console": ConsoleNotifier(console_stream),
        "slack": SlackNotifier(secrets.slack_webhook_url, http),
        "email": EmailNotifier(email, secrets.smtp_password, smtp_factory=smtp_factory),
    }
