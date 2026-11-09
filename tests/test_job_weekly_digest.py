from __future__ import annotations

import httpx
import respx

from automation.config import Secrets
from automation.llm import ANTHROPIC_URL
from automation.runner import run_job
from tests.conftest import NOW  # 2026-10-01 07:00 UTC

A = "https://a.example.com/feed.xml"
B = "https://b.example.com/feed.xml"
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>{title}</title>{items}</channel></rss>"""
ITEM = """<item><title>{title}</title><link>https://example.com/{id}</link><guid>{id}</guid>
<pubDate>{date} 10:00:00 GMT</pubDate><description>About {title}</description></item>"""


def feed(title: str, *posts: tuple[str, str]) -> str:
    """posts: (id, 'Tue, 29 Sep 2026')"""
    return RSS.format(
        title=title, items="".join(ITEM.format(id=i, title=f"Post {i}", date=d) for i, d in posts)
    )


RECENT, RECENT2, OLD = "Tue, 29 Sep 2026", "Mon, 28 Sep 2026", "Mon, 01 Jun 2026"


def services_for(make_settings, make_services, extra: str = "", llm: bool = False):
    config = f'[jobs.weekly-ai-digest]\nfeeds = ["{A}", "{B}"]\n{extra}'
    if llm:
        config = '[llm]\nprovider = "anthropic"\n' + config
    secrets = Secrets(_env_file=None, ANTHROPIC_API_KEY="test-" + "key") if llm else None
    return make_services(make_settings(config), secrets=secrets)


@respx.mock
def test_groups_by_source_and_sends_full_email_and_short_slack(make_settings, make_services, recorders):
    respx.get(A).mock(
        return_value=httpx.Response(200, text=feed("Alpha", ("a1", RECENT), ("a2", RECENT2), ("a0", OLD)))
    )
    respx.get(B).mock(return_value=httpx.Response(200, text=feed("Beta", ("b1", RECENT))))
    services = services_for(
        make_settings, make_services, 'names = { "' + B + '" = "Beta blog" }\nsummarize = false\n'
    )

    assert run_job(services, "weekly-ai-digest", now=NOW).summary == "3 new items sent"
    (email,) = recorders["email"].messages
    assert email.title == "Weekly AI digest: 3 new from 2 sources"
    assert "## Alpha (2 new)" in email.text and "## Beta blog (1 new)" in email.text
    assert "Post a0" not in email.text  # older than lookback_days
    assert email.text.index("Post a1") < email.text.index("Post a2")  # newest first
    assert '<a href="https://example.com/b1">Post b1</a>' in email.html
    (slack,) = recorders["slack"].messages
    assert slack.text.splitlines()[0] == "• Alpha (2): Post a1"
    assert slack.text.endswith("Full digest by email.")

    assert run_job(services, "weekly-ai-digest", now=NOW).summary == "no new items"
    assert len(recorders["email"].messages) == 1


@respx.mock
def test_a_broken_feed_is_reported_and_does_not_stop_the_others(make_settings, make_services, recorders):
    respx.get(A).mock(return_value=httpx.Response(200, text=feed("Alpha", ("a1", RECENT))))
    respx.get(B).mock(return_value=httpx.Response(200, text="<html>not a feed</html>"))
    result = run_job(
        services_for(make_settings, make_services, "summarize = false\n"), "weekly-ai-digest", now=NOW
    )
    assert result.status == "success"
    assert f"Could not read: {B}" in recorders["email"].messages[0].text


@respx.mock
def test_summarises_each_source_and_survives_a_failed_summary(make_settings, make_services, recorders):
    respx.get(A).mock(return_value=httpx.Response(200, text=feed("Alpha", ("a1", RECENT))))
    respx.get(B).mock(return_value=httpx.Response(200, text=feed("Beta", ("b1", RECENT))))
    llm = respx.post(ANTHROPIC_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "Alpha shipped a thing."}],
                    "stop_reason": "end_turn",
                },
            ),
            httpx.Response(400, json={"error": "bad request"}),
        ]
    )
    run_job(services_for(make_settings, make_services, llm=True), "weekly-ai-digest", now=NOW)
    assert llm.call_count == 2
    email = recorders["email"].messages[0]
    assert "## Alpha (1 new)\nAlpha shipped a thing." in email.text
    assert "## Beta (1 new)\n- Post b1" in email.text  # no summary, digest still sent
    assert recorders["slack"].messages[0].text.splitlines()[0] == "• Alpha (1): Alpha shipped a thing."


@respx.mock
def test_dry_run_sends_and_saves_nothing(make_settings, make_services, recorders):
    respx.get(A).mock(return_value=httpx.Response(200, text=feed("Alpha", ("a1", RECENT))))
    respx.get(B).mock(return_value=httpx.Response(200, text=feed("Beta")))
    services = services_for(make_settings, make_services, "summarize = false\n")
    run_job(services, "weekly-ai-digest", now=NOW, dry_run=True)
    assert recorders["email"].messages == [] and recorders["slack"].messages == []
    assert run_job(services, "weekly-ai-digest", now=NOW).summary == "1 new item sent"


@respx.mock
def test_a_failed_slack_summary_never_resends_the_email(make_settings, make_services, recorders):
    from automation.notify import NotifyError

    respx.get(A).mock(return_value=httpx.Response(200, text=feed("Alpha", ("a1", RECENT))))
    respx.get(B).mock(return_value=httpx.Response(200, text=feed("Beta")))
    recorders["slack"].fail = NotifyError("webhook down")
    services = services_for(make_settings, make_services, "summarize = false\n")
    assert run_job(services, "weekly-ai-digest", now=NOW).summary == "1 new item sent; Slack summary failed"
    assert run_job(services, "weekly-ai-digest", now=NOW).summary == "no new items"
    assert len(recorders["email"].messages) == 1
