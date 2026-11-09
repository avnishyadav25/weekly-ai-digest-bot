from __future__ import annotations

import json

import httpx
import pytest
import respx

from automation.config import HttpSettings, LlmSettings, Secrets
from automation.http import HttpClient
from automation.llm import ANTHROPIC_URL, OPENAI_URL, LlmClient, LlmError

KEY = "test-" + "key-value"


@pytest.fixture
def http():
    with HttpClient(HttpSettings(retries=1), sleep=lambda _s: None) as client:
        yield client


def secrets(**values: str) -> Secrets:
    return Secrets(_env_file=None, **{k.upper(): v for k, v in values.items()})


def test_off_unless_provider_and_key(http):
    assert LlmClient.from_settings(LlmSettings(), secrets(), http) is None
    assert LlmClient.from_settings(LlmSettings(provider="anthropic"), secrets(), http) is None
    assert (
        LlmClient.from_settings(LlmSettings(provider="openai"), secrets(anthropic_api_key=KEY), http) is None
    )
    client = LlmClient.from_settings(LlmSettings(provider="anthropic"), secrets(anthropic_api_key=KEY), http)
    assert client is not None
    assert client.model == "claude-opus-5-5"
    openai = LlmClient.from_settings(LlmSettings(provider="openai"), secrets(openai_api_key=KEY), http)
    assert openai.model == "gpt-6-luna"


@respx.mock
def test_anthropic_request_and_response(http):
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [
                    {"type": "thinking", "thinking": ""},
                    {"type": "text", "text": "- point one"},
                    {"type": "text", "text": "\n- point two"},
                ],
                "stop_reason": "end_turn",
            },
        )
    )
    client = LlmClient(LlmSettings(provider="anthropic"), KEY, http)
    assert client.summarize("long text") == "- point one\n- point two"
    request = route.calls.last.request
    assert request.headers["x-api-key"] == KEY
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert request.headers["anthropic-beta"] == "server-side-fallback-2026-07-01"
    body = json.loads(request.read())
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"] == {"effort": "low"}
    assert body["max_tokens"] == 16000
    assert body["messages"][0]["role"] == "user"
    assert "long text" in body["messages"][0]["content"]
    assert body["system"]


@respx.mock
def test_anthropic_without_fallbacks_or_effort(http):
    route = respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200, json={"content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn"}
        )
    )
    client = LlmClient(
        LlmSettings(provider="anthropic", model="claude-haiku-4-5", effort="none", fallbacks=False), KEY, http
    )
    client.complete("system", "prompt")
    request = route.calls.last.request
    body = json.loads(request.read())
    assert "anthropic-beta" not in request.headers
    assert "fallbacks" not in body
    assert "output_config" not in body
    assert body["model"] == "claude-haiku-4-5"


@respx.mock
def test_anthropic_refusal_is_an_error(http):
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "content": [],
                "stop_reason": "refusal",
                "stop_details": {"type": "refusal", "category": "cyber"},
            },
        )
    )
    with pytest.raises(LlmError, match=r"declined.*cyber"):
        LlmClient(LlmSettings(provider="anthropic"), KEY, http).summarize("x")


@respx.mock
def test_overloaded_api_is_retried_then_reported(http):
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(529, text='{"type":"error"}'))
    with pytest.raises(LlmError, match="HTTP 529"):
        LlmClient(LlmSettings(provider="anthropic"), KEY, http).summarize("x")
    assert route.call_count == 2


@respx.mock
def test_openai_request_and_output_parsing(http):
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [
                            {"type": "output_text", "text": "- one"},
                            {"type": "output_text", "text": "\n- two"},
                        ],
                    },
                ],
            },
        )
    )
    client = LlmClient(LlmSettings(provider="openai"), KEY, http)
    assert client.summarize("text") == "- one\n- two"
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.read())
    assert body["model"] == "gpt-6-luna"
    assert body["instructions"]
    assert "text" in body["input"]
    assert body["max_output_tokens"] == 16000


@respx.mock
def test_openai_empty_output_is_an_error(http):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={"status": "incomplete", "output": []}))
    with pytest.raises(LlmError, match="no text"):
        LlmClient(LlmSettings(provider="openai"), KEY, http).summarize("x")


@respx.mock
def test_auth_errors_are_reported_without_the_key(http):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(LlmError) as info:
        LlmClient(LlmSettings(provider="openai"), KEY, http).summarize("x")
    assert "HTTP 401" in str(info.value)
    assert KEY not in str(info.value)
