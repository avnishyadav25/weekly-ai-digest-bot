from __future__ import annotations

import io
import json
from email.utils import format_datetime

import httpx
import pytest
import respx

from automation.config import HttpSettings
from automation.http import HttpClient, retry_after_seconds, safe_url
from automation.logging import setup_logging
from tests.conftest import NOW

URL = "https://api.example.com/items"


@pytest.fixture
def sleeps() -> list[float]:
    return []


@pytest.fixture
def client(sleeps):
    settings = HttpSettings(retries=3, backoff_seconds=0.5, backoff_max_seconds=8, user_agent="test-agent/1")
    with HttpClient(settings, sleep=sleeps.append) as http:
        yield http


@respx.mock
def test_get_retries_5xx_then_succeeds(client, sleeps):
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": True})]
    )
    assert client.get_json(URL) == {"ok": True}
    assert route.call_count == 3
    assert len(sleeps) == 2
    assert all(0 < s <= 8 for s in sleeps)


@respx.mock
def test_gives_up_after_the_last_attempt(client, sleeps):
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        client.get(URL)
    assert route.call_count == 4  # 1 + 3 retries
    response = client.get(URL, retries=0, raise_for_status=False)
    assert response.status_code == 500


@respx.mock
def test_retry_after_is_honoured_and_capped(client, sleeps):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(429, headers={"Retry-After": "120"}),
            httpx.Response(200),
        ]
    )
    client.get(URL)
    assert sleeps == [2.0, 8.0]


def test_retry_after_parses_http_dates():
    later = format_datetime(NOW.replace(second=30), usegmt=True)
    response = httpx.Response(503, headers={"Retry-After": later})
    assert retry_after_seconds(response, now=NOW) == 30
    assert retry_after_seconds(httpx.Response(503)) is None
    assert retry_after_seconds(httpx.Response(503, headers={"Retry-After": "soon"})) is None


@respx.mock
def test_post_is_not_retried_on_5xx(client):
    route = respx.post(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        client.post(URL, json={"a": 1})
    assert route.call_count == 1


@respx.mock
def test_post_is_retried_on_429_and_when_asked(client):
    limited = respx.post(URL).mock(side_effect=[httpx.Response(429), httpx.Response(201, json={"id": 1})])
    assert client.post_json(URL, {"a": 1}) == {"id": 1}
    assert limited.call_count == 2
    respx.post(URL).mock(side_effect=[httpx.Response(500), httpx.Response(200, json={})])
    assert client.post(URL, json={}, retry_unsafe=True).status_code == 200


@respx.mock
def test_connection_errors_are_retried_then_raised(client, sleeps):
    route = respx.get(URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(httpx.ConnectError):
        client.get(URL)
    assert route.call_count == 4
    assert len(sleeps) == 3


@respx.mock
def test_read_timeouts_are_retried_for_get_but_not_post(client):
    get = respx.get(URL).mock(side_effect=[httpx.ReadTimeout("slow"), httpx.Response(200)])
    assert client.get(URL).status_code == 200
    assert get.call_count == 2
    post = respx.post(URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(httpx.ReadTimeout):
        client.post(URL)
    assert post.call_count == 1


@respx.mock
def test_user_agent_and_timeout(client):
    route = respx.get(URL).mock(return_value=httpx.Response(200))
    client.get(URL)
    request = route.calls.last.request
    assert request.headers["user-agent"] == "test-agent/1"
    assert request.extensions["timeout"]["read"] == 10.0


@respx.mock
def test_404_is_not_retried(client):
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    with pytest.raises(httpx.HTTPStatusError):
        client.get(URL)
    assert route.call_count == 1


def test_safe_url_drops_query_and_credentials():
    assert safe_url("https://user:pw@example.com:8443/path?token=abc") == "https://example.com:8443/path"


@respx.mock
def test_retry_logs_never_contain_query_strings_or_secret_urls(client):
    stream = io.StringIO()
    setup_logging("INFO", "json", stream)
    respx.get(f"{URL}?key=abc").mock(side_effect=[httpx.Response(503), httpx.Response(200)])
    client.get(f"{URL}?key=abc")
    respx.post("https://hooks.example.com/secret-path").mock(
        side_effect=[httpx.Response(429), httpx.Response(200)]
    )
    client.post("https://hooks.example.com/secret-path", log_as="webhook")
    logged = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert [line["url"] for line in logged] == [URL, "webhook"]
    assert "key=abc" not in stream.getvalue()
    assert "secret-path" not in stream.getvalue()
