from __future__ import annotations

import io

import pytest

from automation.notify import ConsoleNotifier, Dispatcher, Message, NotifyError
from tests.conftest import Recorder


def test_console_prints_a_readable_block():
    stream = io.StringIO()
    ConsoleNotifier(stream).send(
        Message(
            "Disk almost full",
            "92% used on /var",
            level="error",
            fields={"host": "web-1"},
            link="https://example.com/runbook",
        ),
        job="disk-check",
        run_id="r1",
    )
    assert stream.getvalue() == (
        "[ALERT] Disk almost full\n92% used on /var\nhost: web-1\nhttps://example.com/runbook\n"
        "-- disk-check · run r1\n"
    )


def test_console_is_always_configured():
    assert ConsoleNotifier().configured()[0] is True


def test_dispatcher_sends_to_default_or_given_channels():
    console, slack = Recorder("console"), Recorder("slack")
    dispatcher = Dispatcher({"console": console, "slack": slack}, default=["console"], job="j", run_id="r")
    assert dispatcher.send(Message("a", "b")) == ["console"]
    assert dispatcher.send(Message("c", "d"), ["slack", "console", "slack"]) == ["slack", "console"]
    assert [m.title for m in console.messages] == ["a", "c"]
    assert slack.calls == [{"job": "j", "run_id": "r"}]


def test_one_failing_channel_does_not_stop_the_others():
    broken = Recorder("slack", fail=RuntimeError("webhook down"))
    email = Recorder("email")
    dispatcher = Dispatcher({"slack": broken, "email": email}, default=["slack", "email"])
    with pytest.raises(NotifyError, match="slack: webhook down"):
        dispatcher.send(Message("t", "x"))
    assert len(email.messages) == 1


def test_unconfigured_and_unknown_channels_are_errors():
    dispatcher = Dispatcher({"slack": Recorder("slack", ready=False)}, default=["slack", "pager"])
    with pytest.raises(NotifyError) as info:
        dispatcher.send(Message("t", "x"))
    assert "slack: not configured (set SOMETHING)" in str(info.value)
    assert "pager: unknown channel" in str(info.value)


def test_dry_run_plans_instead_of_sending():
    console, slack = Recorder("console"), Recorder("slack", ready=False)
    dispatcher = Dispatcher({"console": console, "slack": slack}, default=["console", "slack"], dry_run=True)
    assert dispatcher.send(Message("t", "x")) == []
    assert console.messages == []
    assert [(p.channel, p.ready) for p in dispatcher.planned] == [("console", True), ("slack", False)]
