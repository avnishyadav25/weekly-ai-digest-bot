"""Slack notifier: posts to an incoming webhook (Block Kit), URL from ``SLACK_WEBHOOK_URL``.

Format checked against Slack's incoming-webhook and Block Kit docs on 2026-10-01: JSON body with ``text`` (the
notification fallback) and ``blocks``; a 200 response with the body ``ok`` means delivered; header text is
plain_text up to 150 characters; section text up to 3000 characters; up to 10 fields of 2000 characters each.
"""

from __future__ import annotations

from typing import Any

from pydantic import SecretStr

from automation.http import HttpClient
from automation.notify.base import Message, Notifier, NotifyError

_EMOJI = {"info": ":large_green_circle:", "warning": ":large_yellow_circle:", "error": ":red_circle:"}
HEADER_MAX = 150
SECTION_MAX = 3000
FIELD_MAX = 2000
FIELDS_MAX = 10


def escape(text: str) -> str:
    """Escape the three characters Slack's mrkdwn treats as control characters."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


class SlackNotifier(Notifier):
    name = "slack"

    def __init__(self, webhook_url: SecretStr | None, http: HttpClient) -> None:
        self._url = webhook_url
        self._http = http

    def configured(self) -> tuple[bool, str]:
        if self._url is None or not self._url.get_secret_value().strip():
            return False, "set SLACK_WEBHOOK_URL"
        return True, "incoming webhook from SLACK_WEBHOOK_URL"

    def payload(
        self, message: Message, *, job: str | None = None, run_id: str | None = None
    ) -> dict[str, Any]:
        title = f"{_EMOJI[message.level]} {message.title}"
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": _clip(title, HEADER_MAX), "emoji": True},
            },
        ]
        body = escape(message.text.strip())
        if message.link:
            body += f"\n<{message.link}|Open>"
        if body:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": _clip(body, SECTION_MAX)}})
        if message.fields:
            fields = [
                {"type": "mrkdwn", "text": _clip(f"*{escape(k)}*\n{escape(v)}", FIELD_MAX)}
                for k, v in list(message.fields.items())[:FIELDS_MAX]
            ]
            blocks.append({"type": "section", "fields": fields})
        context = " · ".join(
            part for part in (job, f"run {run_id}" if run_id else None, message.level) if part
        )
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": escape(context)}]})
        fallback = f"{message.title}: {message.text.strip()}" if message.text.strip() else message.title
        return {"text": _clip(fallback, SECTION_MAX), "blocks": blocks}

    def send(self, message: Message, *, job: str | None = None, run_id: str | None = None) -> None:
        ready, note = self.configured()
        if not ready or self._url is None:
            raise NotifyError(f"slack is not configured: {note}")
        response = self._http.post(
            self._url.get_secret_value(),
            json=self.payload(message, job=job, run_id=run_id),
            raise_for_status=False,
            log_as="slack webhook",
        )
        if response.status_code != 200 or response.text.strip() != "ok":
            raise NotifyError(
                f"slack webhook returned HTTP {response.status_code}: {response.text.strip()[:200]}"
            )
