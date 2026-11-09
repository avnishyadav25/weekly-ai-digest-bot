from __future__ import annotations

import io
import json

import httpx
import pytest
import respx
from pydantic import SecretStr

from automation.config import HttpSettings
from automation.http import HttpClient
from automation.logging import setup_logging
from automation.notify import Message, NotifyError, SlackNotifier
from automation.notify.slack import HEADER_MAX, SECTION_MAX

# Built at runtime so no webhook-shaped string sits in the source.
WEBHOOK = "https://hooks.slack.com/services/" + "/".join(["T0TEST", "B0TEST", "testtesttest"])


@pytest.fixture
def http():
    with HttpClient(HttpSettings(retries=2), sleep=lambda _s: None) as client:
        yield client


@pytest.fixture
def slack(http):
    return SlackNotifier(SecretStr(WEBHOOK), http)


@respx.mock
def test_posts_block_kit_payload(slack):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(200, text="ok"))
    slack.send(
        Message(
            "1 URL down", "https://example.com is down: HTTP 503", level="error", fields={"since": "07:00"}
        ),
        job="uptime-check",
        run_id="r1",
    )
    body = json.loads(route.calls.last.request.read())
    assert body["text"].startswith("1 URL down")
    header, section, fields, context = body["blocks"]
    assert header["type"] == "header"
    assert header["text"] == {"type": "plain_text", "text": ":red_circle: 1 URL down", "emoji": True}
    assert section["text"]["type"] == "mrkdwn"
    assert "HTTP 503" in section["text"]["text"]
    assert fields["fields"][0]["text"] == "*since*\n07:00"
    assert context["elements"][0]["text"] == "uptime-check · run r1 · error"


def test_escapes_control_characters_and_clips_long_text(slack):
    payload = slack.payload(Message("T" * 200, "a < b & c > d\n" + "x" * 5000))
    header, section, _context = payload["blocks"]
    assert len(header["text"]["text"]) == HEADER_MAX
    assert section["text"]["text"].startswith("a &lt; b &amp; c &gt; d")
    assert len(section["text"]["text"]) == SECTION_MAX


def test_link_is_added_as_a_slack_link(slack):
    section = slack.payload(Message("t", "body", link="https://example.com/run/1"))["blocks"][1]
    assert section["text"]["text"].endswith("<https://example.com/run/1|Open>")


@respx.mock
def test_rate_limit_is_retried(slack):
    route = respx.post(WEBHOOK).mock(side_effect=[httpx.Response(429), httpx.Response(200, text="ok")])
    slack.send(Message("t", "x"))
    assert route.call_count == 2


@respx.mock
@pytest.mark.parametrize(
    ("status", "body"),
    [(400, "invalid_payload"), (403, "action_prohibited"), (404, "channel_not_found"), (200, "unexpected")],
)
def test_slack_errors_raise(slack, status, body):
    respx.post(WEBHOOK).mock(return_value=httpx.Response(status, text=body))
    with pytest.raises(NotifyError, match=body):
        slack.send(Message("t", "x"))


@respx.mock
def test_server_errors_are_not_retried_to_avoid_duplicates(slack):
    route = respx.post(WEBHOOK).mock(return_value=httpx.Response(500, text="oops"))
    with pytest.raises(NotifyError, match="HTTP 500"):
        slack.send(Message("t", "x"))
    assert route.call_count == 1


def test_not_configured_without_the_env_var(http):
    slack = SlackNotifier(None, http)
    assert slack.configured() == (False, "set SLACK_WEBHOOK_URL")
    with pytest.raises(NotifyError, match="SLACK_WEBHOOK_URL"):
        slack.send(Message("t", "x"))


@respx.mock
def test_webhook_url_never_appears_in_logs(slack):
    stream = io.StringIO()
    setup_logging("DEBUG", "json", stream)
    respx.post(WEBHOOK).mock(side_effect=[httpx.Response(429), httpx.Response(200, text="ok")])
    slack.send(Message("t", "x"))
    assert "hooks.slack.com" not in stream.getvalue()
    assert "slack webhook" in stream.getvalue()
