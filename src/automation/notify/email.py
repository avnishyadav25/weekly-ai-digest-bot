"""Email notifier: SMTP with STARTTLS (port 587), implicit TLS (465) or plain (local relays only).

Server, sender and recipients come from ``[notify.email]`` in the config; the password from ``SMTP_PASSWORD``.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import make_msgid
from typing import Any

from pydantic import SecretStr

from automation.config import EmailSettings
from automation.notify.base import Message, Notifier, NotifyError

SmtpFactory = Callable[..., Any]


class EmailNotifier(Notifier):
    name = "email"

    def __init__(
        self,
        settings: EmailSettings,
        password: SecretStr | None,
        *,
        smtp_factory: SmtpFactory | None = None,
        ssl_factory: SmtpFactory | None = None,
    ) -> None:
        self.settings = settings
        self._password = password
        self._smtp = smtp_factory or smtplib.SMTP
        self._smtp_ssl = ssl_factory or smtp_factory or smtplib.SMTP_SSL

    def configured(self) -> tuple[bool, str]:
        s = self.settings
        missing = [name for name, value in (("host", s.host), ("sender", s.sender)) if not value]
        if not s.recipients:
            missing.append("recipients")
        if missing:
            return False, f"set {', '.join(missing)} in [notify.email]"
        if s.username and self._password is None:
            return False, "set SMTP_PASSWORD"
        return True, f"{s.host}:{s.port} ({s.security}) to {', '.join(s.recipients)}"

    def build(self, message: Message, *, job: str | None = None, run_id: str | None = None) -> EmailMessage:
        msg = EmailMessage()
        prefix = "[ALERT] " if message.level == "error" else ""
        msg["Subject"] = f"{prefix}{message.title}"
        msg["From"] = self.settings.sender or ""
        msg["To"] = ", ".join(self.settings.recipients)
        domain = (self.settings.sender or "localhost").rsplit("@", 1)[-1].strip(">")
        msg["Message-ID"] = make_msgid(domain=domain)
        footer = " · ".join(p for p in (job, f"run {run_id}" if run_id else None) if p)
        lines = [message.text.rstrip()]
        lines += [f"{k}: {v}" for k, v in message.fields.items()]
        if message.link:
            lines.append(message.link)
        if footer:
            lines += ["", f"-- {footer}"]
        msg.set_content("\n".join(lines) + "\n")
        if message.html:
            msg.add_alternative(message.html, subtype="html")
        return msg

    def send(self, message: Message, *, job: str | None = None, run_id: str | None = None) -> None:
        ready, note = self.configured()
        if not ready:
            raise NotifyError(f"email is not configured: {note}")
        s = self.settings
        msg = self.build(message, job=job, run_id=run_id)
        context = ssl.create_default_context()
        try:
            if s.security == "ssl":
                server = self._smtp_ssl(s.host, s.port, timeout=s.timeout_seconds, context=context)
            else:
                server = self._smtp(s.host, s.port, timeout=s.timeout_seconds)
            with server as smtp:
                if s.security == "starttls":
                    smtp.starttls(context=context)
                if s.username:
                    smtp.login(s.username, self._password.get_secret_value() if self._password else "")
                smtp.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise NotifyError(f"SMTP {s.host}:{s.port}: {exc}") from exc
