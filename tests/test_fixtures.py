"""Offline mode: the fixture transport behind ``automation --offline``."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest

from automation.config import HttpSettings
from automation.http import FixtureError, FixtureTransport, HttpClient, load_fixtures
from automation.jobs import discover_jobs
from automation.runner import run_job
from tests.conftest import FIXTURES, NOW


def offline_client(directory=FIXTURES) -> HttpClient:
    return HttpClient(
        HttpSettings(), transport=FixtureTransport(directory, now=lambda: NOW), sleep=lambda _s: None
    )


def test_shipped_fixtures_cover_every_example_job():
    routes = load_fixtures(FIXTURES)
    sources = {route.source.stem for route in routes}
    assert sources == set(discover_jobs())


def test_serves_json_body_files_and_status_codes():
    with offline_client() as http:
        assert http.get("https://example.com/", raise_for_status=False).status_code == 200
        assert http.get("https://status.example.com/health", raise_for_status=False).status_code == 503
        feed = http.get("https://example.com/blog/feed.xml").text
        assert "<rss" in feed
        prs = http.get_json(
            "https://api.github.com/repos/your-org/your-repo/pulls", params={"state": "closed"}
        )
        assert prs[0]["number"] == 42


def test_time_templates_are_rendered_relative_to_now():
    with offline_client() as http:
        prs = http.get_json("https://api.github.com/repos/your-org/your-repo/pulls")
        feed = http.get("https://example.com/blog/feed.xml").text
    assert prs[0]["merged_at"] == (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z")
    assert "Wed, 30 Sep 2026 07:00:00 GMT" in feed
    assert "{{" not in feed


def test_unknown_request_is_a_clear_error():
    with (
        offline_client() as http,
        pytest.raises(FixtureError, match=re.escape("no fixture matches GET https://example.org/x")),
    ):
        http.get("https://example.org/x?token=secret")


def test_glob_and_exact_query_routes(tmp_path):
    (tmp_path / "a.json").write_text(
        json.dumps(
            {
                "routes": [
                    {"url": "https://example.com/items?page=2", "json": {"page": 2}},
                    {"url": "https://example.com/files/*", "body": "any file"},
                    {"method": "POST", "url": "https://example.com/hook", "status": 204},
                ]
            }
        ),
        encoding="utf-8",
    )
    with offline_client(tmp_path) as http:
        assert http.get_json("https://example.com/items?page=2") == {"page": 2}
        assert http.get("https://example.com/files/x/y.txt").text == "any file"
        assert http.post("https://example.com/hook").status_code == 204
        with pytest.raises(FixtureError):
            http.get("https://example.com/items?page=3")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("{not json", "invalid JSON"),
        ('{"other": []}', '"routes" list'),
        ('{"routes": [{"status": 200}]}', "needs a url"),
        ('{"routes": [{"url": "https://x.example", "body_file": "missing.xml"}]}', "not found"),
    ],
)
def test_invalid_fixture_files_are_reported(tmp_path, content, message):
    (tmp_path / "bad.json").write_text(content, encoding="utf-8")
    with pytest.raises(FixtureError, match=message):
        load_fixtures(tmp_path)


def test_missing_folder(tmp_path):
    with pytest.raises(FixtureError, match="fixtures folder not found"):
        FixtureTransport(tmp_path / "nope")


def test_offline_run_of_every_example_job(examples, make_services, recorders):
    from automation.config import load_settings

    settings = load_settings(examples / "config.example.toml", env_file=None)
    services = make_services(settings, offline=True)
    assert isinstance(services.http._client._transport, FixtureTransport)
    for name in discover_jobs():
        outcome = run_job(services, name)  # real time: the fixtures render dates relative to it
        assert outcome.status == "success", (name, outcome.error)
    titles = [m.title for m in recorders["console"].messages]
    assert "uptime-check: 1 URL down" in titles
    assert any(t.startswith("New posts: 3 new") for t in titles)
    assert any(t.startswith("Weekly orders by region") for t in titles)
    assert any(t.startswith("Merged pull requests: 3 in 2 repos") for t in titles)
    # notifications go through a separate live client in offline mode (the recorders replace it here)
    assert services.notify_http is not services.http
