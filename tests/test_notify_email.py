from __future__ import annotations

import smtplib
from typing import Any, ClassVar

import pytest
from pydantic import SecretStr

from automation.config import EmailSettings
from automation.notify import EmailNotifier, Message, NotifyError


class FakeSMTP:
    """Stands in for smtplib.SMTP / SMTP_SSL: records the conversation, sends nothing."""

    instances: ClassVar[list[FakeSMTP]] = []
    fail_on: ClassVar[str | None] = None

    def __init__(self, host: str, port: int, timeout: float = 0, **kwargs: Any) -> None:
        self.host, self.port, self.timeout, self.kwargs = host, port, timeout, kwargs
        self.calls: list[str] = []
        self.sent: list[Any] = []
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def _maybe_fail(self, step: str) -> None:
        if FakeSMTP.fail_on == step:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    def starttls(self, context: Any = None) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self._maybe_fail("login")
        self.calls.append(f"login {user} {password}")

    def send_message(self, msg: Any) -> None:
        self.calls.append("send")
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def reset_fake():
    FakeSMTP.instances = []
    FakeSMTP.fail_on = None


def settings(**overrides: Any) -> EmailSettings:
    base = {
        "host": "smtp.example.com",
        "port": 587,
        "username": "alerts@example.com",
        "sender": "Automation <alerts@example.com>",
        "recipients": ["ops@example.com", "dev@example.com"],
    }
    return EmailSettings(**{**base, **overrides})


def test_starttls_login_and_send():
    notifier = EmailNotifier(settings(), SecretStr("pw-from-env"), smtp_factory=FakeSMTP)
    notifier.send(Message("Report", "plain body", html="<p>html body</p>"), job="csv-report", run_id="r1")
    (smtp,) = FakeSMTP.instances
    assert (smtp.host, smtp.port, smtp.timeout) == ("smtp.example.com", 587, 30.0)
    assert smtp.calls == ["starttls", "login alerts@example.com pw-from-env", "send", "quit"]
    msg = smtp.sent[0]
    assert msg["Subject"] == "Report"
    assert msg["To"] == "ops@example.com, dev@example.com"
    assert msg["From"] == "Automation <alerts@example.com>"
    plain = msg.get_body(("plain",)).get_content()
    assert "plain body" in plain
    assert "-- csv-report · run r1" in plain
    assert msg.get_body(("html",)).get_content().strip() == "<p>html body</p>"


def test_error_messages_get_an_alert_prefix():
    notifier = EmailNotifier(settings(), SecretStr("pw"), smtp_factory=FakeSMTP)
    assert notifier.build(Message("job failed", "x", level="error"))["Subject"] == "[ALERT] job failed"


def test_implicit_tls_uses_the_ssl_factory():
    notifier = EmailNotifier(settings(port=465, security="ssl"), SecretStr("pw"), ssl_factory=FakeSMTP)
    notifier.send(Message("t", "x"))
    (smtp,) = FakeSMTP.instances
    assert "context" in smtp.kwargs
    assert "starttls" not in smtp.calls


def test_plain_relay_without_login():
    notifier = EmailNotifier(settings(security="none", port=25, username=None), None, smtp_factory=FakeSMTP)
    notifier.send(Message("t", "x"))
    assert FakeSMTP.instances[0].calls == ["send", "quit"]


@pytest.mark.parametrize(
    ("overrides", "password", "note"),
    [
        ({"host": None}, SecretStr("pw"), "set host"),
        ({"recipients": []}, SecretStr("pw"), "recipients"),
        ({}, None, "set SMTP_PASSWORD"),
    ],
)
def test_not_configured(overrides, password, note):
    notifier = EmailNotifier(settings(**overrides), password, smtp_factory=FakeSMTP)
    ready, message = notifier.configured()
    assert not ready
    assert note in message
    with pytest.raises(NotifyError, match="email is not configured"):
        notifier.send(Message("t", "x"))
    assert FakeSMTP.instances == []


def test_smtp_errors_become_notify_errors():
    FakeSMTP.fail_on = "login"
    notifier = EmailNotifier(settings(), SecretStr("pw"), smtp_factory=FakeSMTP)
    with pytest.raises(NotifyError, match=r"SMTP smtp.example.com:587: \(535"):
        notifier.send(Message("t", "x"))
