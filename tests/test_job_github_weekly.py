from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from automation.config import Secrets
from automation.integrations.github import API_VERSION
from automation.runner import run_job
from tests.conftest import NOW

API = "https://api.github.com/repos"


def pr(number: int, days_ago: float, merged: bool = True, title: str | None = None) -> dict:
    when = (NOW - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {
        "number": number,
        "title": title or f"Change {number}",
        "html_url": f"https://github.com/acme/app/pull/{number}",
        "user": {"login": "dev"},
        "updated_at": when,
        "merged_at": when if merged else None,
    }


@pytest.fixture
def services(make_settings, make_services):
    return make_services(make_settings('[jobs.github-weekly]\nrepos = ["acme/app", "acme/lib"]\n'))


@respx.mock
def test_digest_of_merged_prs_in_the_window(services, recorders):
    app = respx.get(f"{API}/acme/app/pulls").mock(
        return_value=httpx.Response(
            200, json=[pr(5, 1), pr(4, 2, merged=False), pr(3, 6.5), pr(2, 9), pr(1, 20)]
        )
    )
    respx.get(f"{API}/acme/lib/pulls").mock(return_value=httpx.Response(200, json=[]))
    outcome = run_job(services, "github-weekly", now=NOW)
    assert outcome.summary == "2 merged pull requests sent"
    message = recorders["console"].messages[0]
    assert message.title == "Merged pull requests: 2 in 2 repos"
    assert "#5 Change 5 by dev" in message.text
    assert "#3 Change 3" in message.text
    assert "#2" not in message.text and "#4" not in message.text
    request = app.calls.last.request
    assert request.url.params["state"] == "closed"
    assert request.url.params["sort"] == "updated"
    assert request.headers["x-github-api-version"] == API_VERSION
    assert request.headers["accept"] == "application/vnd.github+json"
    assert "authorization" not in request.headers


@respx.mock
def test_follows_pagination_until_older_updates(services):
    page2 = f"{API}/acme/app/pulls?page=2"
    respx.get(page2).mock(return_value=httpx.Response(200, json=[pr(1, 3), pr(0, 30)]))
    first = respx.get(f"{API}/acme/app/pulls", params={"state": "closed"}).mock(
        return_value=httpx.Response(200, json=[pr(2, 1)], headers={"Link": f'<{page2}>; rel="next"'})
    )
    respx.get(f"{API}/acme/lib/pulls").mock(return_value=httpx.Response(200, json=[]))
    assert run_job(services, "github-weekly", now=NOW).data["merged"] == 2
    assert first.call_count == 1


@respx.mock
def test_each_pr_is_reported_once(services, recorders):
    route = respx.get(f"{API}/acme/app/pulls").mock(return_value=httpx.Response(200, json=[pr(1, 1)]))
    respx.get(f"{API}/acme/lib/pulls").mock(return_value=httpx.Response(200, json=[]))
    run_job(services, "github-weekly", now=NOW)
    assert run_job(services, "github-weekly", now=NOW).summary == "no newly merged pull requests"
    route.mock(return_value=httpx.Response(200, json=[pr(2, 0.5), pr(1, 1)]))
    run_job(services, "github-weekly", now=NOW)
    assert "#2" in recorders["console"].messages[-1].text
    assert "#1 " not in recorders["console"].messages[-1].text


@respx.mock
def test_token_is_sent_when_set(make_settings, make_services):
    route = respx.get(f"{API}/acme/app/pulls").mock(return_value=httpx.Response(200, json=[]))
    services = make_services(
        make_settings('[jobs.github-weekly]\nrepos = ["acme/app"]\n'),
        secrets=Secrets(_env_file=None, GITHUB_TOKEN="test-" + "token"),
    )
    run_job(services, "github-weekly", now=NOW)
    assert route.calls.last.request.headers["authorization"] == "Bearer test-token"


@respx.mock
@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(404), "acme/app: not found"),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), "rate limit reached; set GITHUB_TOKEN"),
        (httpx.Response(401, text="Bad credentials"), "HTTP 401"),
    ],
)
def test_api_errors_fail_the_run_clearly(services, response, error):
    respx.get(f"{API}/acme/app/pulls").mock(return_value=response)
    outcome = run_job(services, "github-weekly", now=NOW)
    assert outcome.status == "failed"
    assert error in outcome.error


@respx.mock
def test_send_empty(make_settings, make_services, recorders):
    respx.get(f"{API}/acme/app/pulls").mock(return_value=httpx.Response(200, json=[]))
    services = make_services(make_settings('[jobs.github-weekly]\nrepos = ["acme/app"]\nsend_empty = true\n'))
    run_job(services, "github-weekly", now=NOW)
    assert "Nothing merged in the last 7 days." in recorders["console"].messages[0].text


def test_repo_names_are_validated(make_settings, make_services):
    from automation.config import ConfigError

    services = make_services(make_settings('[jobs.github-weekly]\nrepos = ["just-a-name"]\n'))
    with pytest.raises(ConfigError, match="owner/name"):
        run_job(services, "github-weekly", now=NOW)
