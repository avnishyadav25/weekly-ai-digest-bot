"""Optional text summaries through the Anthropic or OpenAI HTTP API. Off unless ``[llm] provider`` is set and the
matching key (``ANTHROPIC_API_KEY`` or ``OPENAI_API_KEY``) is in the environment.

API details checked against the vendors' documentation on 2026-10-01:

- Anthropic Messages API: ``POST https://api.anthropic.com/v1/messages`` with ``x-api-key`` and
  ``anthropic-version: 2023-06-01``. Default model ``claude-opus-5-5`` (the model Anthropic's models overview
  recommends starting with; Claude Opus 5 is listed as legacy) with ``output_config.effort`` (``low`` here,
  enough for a summary; the model's own default is ``medium``). With ``fallbacks = true`` the request carries
  ``fallbacks: "default"`` and the beta header ``server-side-fallback-2026-07-01``, so a request the model declines
  is re-run on Anthropic's recommended fallback model; a response that still ends with ``stop_reason: "refusal"``
  raises :class:`LlmError`.
- OpenAI Responses API: ``POST https://api.openai.com/v1/responses`` with a bearer key. Default model
  ``gpt-6-luna`` (OpenAI's model for focused, high-volume tasks). The text is collected from every
  ``output_text`` part of ``output``.

Change ``[llm] model`` to use another model. For a model without effort or server-side fallbacks, set
``effort = "none"`` and ``fallbacks = false``.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from automation.config import LlmSettings, Secrets
from automation.http import HttpClient

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
OPENAI_URL = "https://api.openai.com/v1/responses"
DEFAULT_MODELS = {"anthropic": "claude-opus-5-5", "openai": "gpt-6-luna"}

SUMMARY_SYSTEM = (
    "You summarise automated reports for a busy reader. Write plain text, no Markdown headings. "
    "Use at most five short bullet points starting with '- '. Only state what the input says."
)

log = logging.getLogger("automation.llm")


class LlmError(Exception):
    """The provider returned an error, declined the request, or returned no text."""


class LlmClient:
    def __init__(self, settings: LlmSettings, api_key: str, http: HttpClient) -> None:
        if settings.provider is None:
            raise ValueError("LlmClient needs [llm] provider")
        self.settings = settings
        self.provider = settings.provider
        self.model = settings.model or DEFAULT_MODELS[settings.provider]
        self._key = api_key
        self._http = http

    @classmethod
    def from_settings(cls, settings: LlmSettings, secrets: Secrets, http: HttpClient) -> LlmClient | None:
        """A client, or None when no provider is set or its key is missing."""
        if settings.provider is None:
            return None
        secret = secrets.anthropic_api_key if settings.provider == "anthropic" else secrets.openai_api_key
        if secret is None or not secret.get_secret_value().strip():
            return None
        return cls(settings, secret.get_secret_value(), http)

    def complete(self, system: str, prompt: str) -> str:
        """One request, one text answer."""
        try:
            if self.provider == "anthropic":
                return self._anthropic(system, prompt)
            return self._openai(system, prompt)
        except httpx.HTTPStatusError as exc:
            detail = exc.response.text[:300]
            raise LlmError(f"{self.provider} API returned HTTP {exc.response.status_code}: {detail}") from exc
        except httpx.TransportError as exc:
            raise LlmError(f"{self.provider} API unreachable: {exc}") from exc

    def summarize(self, text: str, instruction: str = "Summarise the main points of this digest.") -> str:
        return self.complete(SUMMARY_SYSTEM, f"{instruction}\n\n<input>\n{text}\n</input>")

    # -- providers ------------------------------------------------------------------------------------------

    def _anthropic(self, system: str, prompt: str) -> str:
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.settings.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        headers = {
            "x-api-key": self._key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        if self.settings.effort != "none":
            body["output_config"] = {"effort": self.settings.effort}
        if self.settings.fallbacks:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = ANTHROPIC_FALLBACK_BETA
        data = self._http.post(
            ANTHROPIC_URL,
            json=body,
            headers=headers,
            retry_unsafe=True,
            timeout=self.settings.timeout_seconds,
        ).json()
        stop = data.get("stop_reason")
        if stop == "refusal":
            category = (data.get("stop_details") or {}).get("category")
            raise LlmError(f"the model declined the request (refusal{f', {category}' if category else ''})")
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if stop == "max_tokens":
            log.warning("llm output cut at max_tokens", extra={"model": self.model})
        if not text:
            raise LlmError(f"no text in the response (stop_reason {stop})")
        return text

    def _openai(self, system: str, prompt: str) -> str:
        body = {
            "model": self.model,
            "instructions": system,
            "input": prompt,
            "max_output_tokens": self.settings.max_tokens,
        }
        headers = {"authorization": f"Bearer {self._key}", "content-type": "application/json"}
        data = self._http.post(
            OPENAI_URL, json=body, headers=headers, retry_unsafe=True, timeout=self.settings.timeout_seconds
        ).json()
        parts = [
            part.get("text", "")
            for item in data.get("output", [])
            if item.get("type") == "message"
            for part in item.get("content", [])
            if part.get("type") == "output_text"
        ]
        text = "".join(parts).strip()
        if data.get("status") == "incomplete":
            reason = (data.get("incomplete_details") or {}).get("reason")
            log.warning("llm output incomplete", extra={"model": self.model, "reason": reason})
        if not text:
            raise LlmError(f"no text in the response (status {data.get('status')})")
        return text
