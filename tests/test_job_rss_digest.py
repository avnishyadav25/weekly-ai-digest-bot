from __future__ import annotations

import json

import httpx
import pytest
import respx

from automation.config import Secrets
from automation.integrations.rss import FeedError, parse_feed
from automation.llm import ANTHROPIC_URL
from automation.runner import run_job
from tests.conftest import NOW

FEED = "https://example.com/feed.xml"
RSS = """<?xml version="1.0"?><rss version="2.0"><channel><title>Example</title>
{items}
</channel></rss>"""
ITEM = """<item><title>{title}</title><link>https://example.com/{n}</link><guid>id-{n}</guid>
<pubDate>Wed, 0{n} Sep 2026 10:00:00 GMT</pubDate><description>&lt;b&gt;Body&lt;/b&gt; {n}</description></item>"""
ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom example</title>
<entry><title>First</title><id>urn:uuid:1</id><link rel="alternate" href="https://example.com/a1"/>
<updated>2026-09-30T10:00:00Z</updated><summary>One</summary></entry>
<entry><title>Second</title><id>urn:uuid:2</id><link href="https://example.com/a2"/>
<published>2026-09-29T10:00:00+02:00</published></entry></feed>"""
RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel><title>RDF example</title></channel>
<item><title>Old style</title><link>https://example.com/r1</link><dc:date>2026-09-28T08:00:00Z</dc:date></item>
</rdf:RDF>"""


def rss(*ns: int) -> str:
    return RSS.format(items="".join(ITEM.format(n=n, title=f"Post {n}") for n in ns))


def services_for(make_settings, make_services, extra: str = "", secrets: Secrets | None = None):
    config = f'[jobs.rss-digest]\nfeeds = ["{FEED}"]\n{extra}'
    return make_services(make_settings(config), secrets=secrets)


def test_parses_rss_atom_and_rdf():
    feed = parse_feed(rss(1, 2), FEED)
    assert feed.title == "Example"
    assert [i.id for i in feed.items] == ["id-1", "id-2"]
    assert feed.items[0].summary == "Body 1"
    assert feed.items[0].published.isoformat() == "2026-09-01T10:00:00+00:00"
    atom = parse_feed(ATOM)
    assert [(i.title, i.link) for i in atom.items] == [
        ("First", "https://example.com/a1"),
        ("Second", "https://example.com/a2"),
    ]
    assert atom.items[1].published.utcoffset().total_seconds() == 7200
    rdf = parse_feed(RDF)
    assert rdf.title == "RDF example"
    assert rdf.items[0].id == "https://example.com/r1"


@pytest.mark.parametrize("body", ["not xml", "<html><body>hi</body></html>"])
def test_rejects_non_feeds(body):
    with pytest.raises(FeedError):
        parse_feed(body, FEED)


@respx.mock
def test_sends_new_items_once(make_settings, make_services, recorders):
    route = respx.get(FEED).mock(return_value=httpx.Response(200, text=rss(1, 2)))
    services = services_for(make_settings, make_services)
    assert run_job(services, "rss-digest", now=NOW).summary == "2 new items sent"
    (digest,) = recorders["console"].messages
    assert digest.title == "New posts: 2 new"
    assert digest.text.index("Post 2") < digest.text.index("Post 1")  # newest first
    assert '<a href="https://example.com/2">Post 2</a>' in digest.html

    assert run_job(services, "rss-digest", now=NOW).summary == "no new items"
    route.mock(return_value=httpx.Response(200, text=rss(1, 2, 3)))
    assert run_job(services, "rss-digest", now=NOW).summary == "1 new item sent"
    assert "Post 3" in recorders["console"].messages[-1].text
    assert "Post 1" not in recorders["console"].messages[-1].text


@respx.mock
def test_caps_the_digest_and_counts_the_rest(make_settings, make_services, recorders):
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rss(1, 2, 3, 4, 5)))
    run_job(services_for(make_settings, make_services, "max_items = 2\n"), "rss-digest", now=NOW)
    text = recorders["console"].messages[0].text
    assert text.count("- Post") == 2
    assert "... and 3 more" in text


@respx.mock
def test_first_run_skip_marks_existing_items(make_settings, make_services, recorders):
    route = respx.get(FEED).mock(return_value=httpx.Response(200, text=rss(1, 2)))
    services = services_for(make_settings, make_services, 'first_run = "skip"\n')
    assert "marked 2 existing items" in run_job(services, "rss-digest", now=NOW).summary
    assert recorders["console"].messages == []
    route.mock(return_value=httpx.Response(200, text=rss(1, 2, 3)))
    run_job(services, "rss-digest", now=NOW)
    assert recorders["console"].messages[0].title == "New posts: 1 new"


@respx.mock
def test_items_are_not_marked_seen_when_sending_fails(make_settings, make_services, recorders):
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rss(1)))
    services = services_for(make_settings, make_services)
    recorders["console"].fail = RuntimeError("down")
    assert run_job(services, "rss-digest", now=NOW).status == "failed"
    recorders["console"].fail = None
    assert run_job(services, "rss-digest", now=NOW).summary == "1 new item sent"


@respx.mock
def test_summary_from_the_llm_when_configured(make_settings, make_services, recorders):
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rss(1, 2)))
    llm = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": "- Two posts about examples."}],
                "stop_reason": "end_turn",
            },
        )
    )
    secrets = Secrets(_env_file=None, ANTHROPIC_API_KEY="test-" + "key")
    services = make_services(
        make_settings(
            f'[llm]\nprovider = "anthropic"\n[jobs.rss-digest]\nfeeds = ["{FEED}"]\nsummarize = true\n'
        ),
        secrets=secrets,
    )
    run_job(services, "rss-digest", now=NOW)
    digest = recorders["console"].messages[0]
    assert digest.text.startswith("- Two posts about examples.\n\n- Post 2")
    assert "Post 1" in json.loads(llm.calls.last.request.read())["messages"][0]["content"]


@respx.mock
def test_digest_still_goes_out_when_the_summary_fails(make_settings, make_services, recorders):
    respx.get(FEED).mock(return_value=httpx.Response(200, text=rss(1)))
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(400, json={"error": "bad request"}))
    secrets = Secrets(_env_file=None, ANTHROPIC_API_KEY="test-" + "key")
    services = make_services(
        make_settings(
            f'[llm]\nprovider = "anthropic"\n[jobs.rss-digest]\nfeeds = ["{FEED}"]\nsummarize = true\n'
        ),
        secrets=secrets,
    )
    assert run_job(services, "rss-digest", now=NOW).status == "success"
    assert recorders["console"].messages[0].text.startswith("- Post 1")


@respx.mock
def test_a_broken_feed_fails_the_run(make_settings, make_services):
    respx.get(FEED).mock(return_value=httpx.Response(200, text="<html>moved</html>"))
    outcome = run_job(services_for(make_settings, make_services), "rss-digest", now=NOW)
    assert outcome.status == "failed"
    assert "not an RSS or Atom feed" in outcome.error
