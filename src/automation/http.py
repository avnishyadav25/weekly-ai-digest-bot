"""HTTP client for jobs: timeouts, a user agent, retries with backoff, JSON helpers, and an offline mode.

Retries (tenacity): transport errors and HTTP 408, 429, 500, 502, 503, 504 and 529 are retried with exponential
backoff and jitter; a ``Retry-After`` header is honoured (capped at ``backoff_max_seconds``). Requests that are not
idempotent (POST, PATCH) are retried only on 429 and on connection errors, where the server cannot have acted on
them; pass ``retry_unsafe=True`` when repeating the request is harmless.

Offline mode (``automation --offline``) swaps the transport for :class:`FixtureTransport`, which answers from JSON
files in ``examples/fixtures/`` and never opens a socket.
"""

from __future__ import annotations

import email.utils
import fnmatch
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from tenacity import RetryCallState, Retrying, stop_after_attempt, wait_exponential_jitter

from automation.config import HttpSettings

RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PUT", "DELETE"})

log = logging.getLogger("automation.http")


def safe_url(url: str | httpx.URL) -> str:
    """The URL without query string or credentials, for logs (query strings often carry tokens)."""
    parts = urlsplit(str(url))
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def retry_after_seconds(response: httpx.Response, now: datetime | None = None) -> float | None:
    """Seconds to wait from a ``Retry-After`` header (delta-seconds or an HTTP date), or None."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - (now or datetime.now(UTC))).total_seconds())


class HttpClient:
    """A thin wrapper over ``httpx.Client``. Thread-safe; share one per process."""

    def __init__(
        self,
        settings: HttpSettings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.settings = settings or HttpSettings()
        self.offline = isinstance(transport, FixtureTransport)
        self._sleep = sleep
        self._backoff = wait_exponential_jitter(
            initial=self.settings.backoff_seconds,
            max=self.settings.backoff_max_seconds,
            jitter=self.settings.backoff_seconds,
        )
        self._client = httpx.Client(
            timeout=httpx.Timeout(self.settings.timeout_seconds),
            headers={"User-Agent": self.settings.user_agent, **(headers or {})},
            transport=transport,
            follow_redirects=True,
        )

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def _wait(self, state: RetryCallState) -> float:
        outcome = state.outcome
        if outcome is not None and not outcome.failed:
            delay = retry_after_seconds(outcome.result())
            if delay is not None:
                return min(delay, self.settings.backoff_max_seconds)
        return self._backoff(state)

    def request(
        self,
        method: str,
        url: str,
        *,
        retries: int | None = None,
        retry_unsafe: bool = False,
        raise_for_status: bool = True,
        log_as: str | None = None,
        **kwargs: Any,
    ) -> httpx.Response:
        """Send a request with retries. Returns the final response; raises ``httpx.HTTPStatusError`` for a
        4xx/5xx final response when ``raise_for_status`` is true, and ``httpx.TransportError`` when the server
        could not be reached after the last attempt.

        ``log_as`` replaces the URL in log lines (use it for URLs that are secrets, such as Slack webhooks).
        """
        method = method.upper()
        attempts = (self.settings.retries if retries is None else retries) + 1
        repeatable = method in IDEMPOTENT_METHODS or retry_unsafe
        label = log_as or safe_url(url)

        def should_retry(state: RetryCallState) -> bool:
            outcome = state.outcome
            if outcome is None:
                return False
            if outcome.failed:
                exc = outcome.exception()
                if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
                    return True
                return repeatable and isinstance(exc, httpx.TransportError)
            status = outcome.result().status_code
            return status == 429 or (repeatable and status in RETRY_STATUSES)

        def before_sleep(state: RetryCallState) -> None:
            outcome = state.outcome
            reason = (
                f"{type(outcome.exception()).__name__}"
                if outcome is not None and outcome.failed
                else f"HTTP {outcome.result().status_code}"
                if outcome is not None
                else "unknown"
            )
            wait = state.next_action.sleep if state.next_action else 0
            log.warning(
                "http retry",
                extra={
                    "method": method,
                    "url": label,
                    "reason": reason,
                    "attempt": state.attempt_number,
                    "wait_seconds": round(wait, 2),
                },
            )

        retrying = Retrying(
            stop=stop_after_attempt(attempts),
            wait=self._wait,
            retry=should_retry,
            sleep=self._sleep,
            before_sleep=before_sleep,
            retry_error_callback=lambda state: state.outcome.result() if state.outcome else None,
        )
        response: httpx.Response = retrying(self._client.request, method, url, **kwargs)
        log.debug("http", extra={"method": method, "url": label, "status": response.status_code})
        if raise_for_status:
            response.raise_for_status()
        return response

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)

    def get_json(self, url: str, **kwargs: Any) -> Any:
        return self.get(url, **kwargs).json()

    def post_json(self, url: str, payload: Any, **kwargs: Any) -> Any:
        return self.post(url, json=payload, **kwargs).json()


# ---------------------------------------------------------------------------------------------------------------
# Offline fixtures


class FixtureError(RuntimeError):
    """A fixture file is invalid, or no fixture matches a request made in offline mode."""


_UNITS = {"d": "days", "h": "hours", "m": "minutes"}
_TEMPLATE = re.compile(r"\{\{\s*now(?:\s*([+-])\s*(\d+)\s*([dhm]))?(?::(rfc822))?\s*\}\}")


def _render(text: str, now: datetime) -> str:
    """Replace ``{{now}}``, ``{{now-2d}}``, ``{{now+6h}}``, ``{{now-30m:rfc822}}`` with timestamps (UTC)."""

    def sub(match: re.Match[str]) -> str:
        sign, amount, unit, fmt = match.groups()
        moment = now
        if amount:
            delta = timedelta(**{_UNITS[unit]: int(amount)})
            moment = now + delta if sign == "+" else now - delta
        if fmt == "rfc822":
            return email.utils.format_datetime(moment.astimezone(UTC), usegmt=True)
        return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

    return _TEMPLATE.sub(sub, text)


@dataclass(frozen=True)
class FixtureRoute:
    method: str
    url: str
    status: int
    headers: dict[str, str]
    body: str | None
    json_body: Any
    body_file: Path | None
    source: Path

    def matches(self, request: httpx.Request) -> bool:
        if self.method not in (request.method, "*"):
            return False
        full = str(request.url)
        if "*" in self.url:
            return fnmatch.fnmatchcase(full, self.url)
        if "?" in self.url:
            return full == self.url
        return full.split("?", 1)[0] == self.url

    def response(self, request: httpx.Request, now: datetime) -> httpx.Response:
        headers = dict(self.headers)
        if self.json_body is not None:
            content = _render(json.dumps(self.json_body), now).encode()
            headers.setdefault("content-type", "application/json")
        elif self.body_file is not None:
            content = _render(self.body_file.read_text(encoding="utf-8"), now).encode()
        else:
            content = _render(self.body or "", now).encode()
        return httpx.Response(self.status, headers=headers, content=content, request=request)


def load_fixtures(directory: Path) -> list[FixtureRoute]:
    """Read every ``*.json`` fixture file in ``directory``. Format: ``{"routes": [{"method": "GET", "url": ...,
    "status": 200, "headers": {...}, "body": "..." | "json": {...} | "body_file": "name.xml"}]}``."""
    if not directory.is_dir():
        raise FixtureError(f"fixtures folder not found: {directory}")
    routes: list[FixtureRoute] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FixtureError(f"{path}: invalid JSON: {exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("routes"), list):
            raise FixtureError(f'{path}: expected an object with a "routes" list')
        for i, raw in enumerate(data["routes"]):
            if not isinstance(raw, dict) or not raw.get("url"):
                raise FixtureError(f"{path}: routes[{i}] needs a url")
            body_file = raw.get("body_file")
            if body_file is not None and not (directory / body_file).is_file():
                raise FixtureError(f"{path}: routes[{i}] body_file {body_file} not found")
            routes.append(
                FixtureRoute(
                    method=str(raw.get("method", "GET")).upper(),
                    url=str(raw["url"]),
                    status=int(raw.get("status", 200)),
                    headers={str(k): str(v) for k, v in raw.get("headers", {}).items()},
                    body=raw.get("body"),
                    json_body=raw.get("json"),
                    body_file=directory / body_file if body_file else None,
                    source=path,
                )
            )
    return routes


class FixtureTransport(httpx.BaseTransport):
    """An httpx transport that serves canned responses and never touches the network."""

    def __init__(self, directory: Path, now: Callable[[], datetime] | None = None) -> None:
        self.directory = directory
        self.routes = load_fixtures(directory)
        self._now = now or (lambda: datetime.now(UTC))
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for route in self.routes:
            if route.matches(request):
                return route.response(request, self._now())
        raise FixtureError(
            f"offline mode: no fixture matches {request.method} {safe_url(request.url)}; "
            f"add a route to a JSON file in {self.directory}"
        )
