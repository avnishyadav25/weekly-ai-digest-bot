"""Shared fixtures. Every test runs in an empty temporary working directory, without AUTOMATION_* or secret
environment variables, and with sockets blocked: a test that tries to reach the network fails."""

from __future__ import annotations

import io
import logging
import os
import shutil
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from automation.config import Secrets, Settings, load_settings
from automation.http import HttpClient
from automation.jobs import Job, discover_jobs
from automation.notify.base import Message, Notifier
from automation.runner import Services, build_services
from automation.state import StateStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = PROJECT_ROOT / "examples" / "config.example.toml"
FIXTURES = PROJECT_ROOT / "examples" / "fixtures"
SECRET_ENV = ("SLACK_WEBHOOK_URL", "SMTP_PASSWORD", "GITHUB_TOKEN", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")
NOW = datetime(2026, 10, 1, 7, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.startswith("AUTOMATION_") or name.upper() in SECRET_ENV:
            monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def reset_logging():
    """Remove the handler setup_logging() installs, so no test writes to another test's captured stream."""
    yield
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == "automation":
            root.removeHandler(handler)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("tests must not open network connections; mock HTTP with respx")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def examples(tmp_path: Path) -> Path:
    """A copy of examples/ in the working directory, so relative paths in the example config resolve."""
    shutil.copytree(PROJECT_ROOT / "examples", tmp_path / "examples")
    return tmp_path / "examples"


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    def write(text: str, name: str = "config.toml") -> Path:
        path = tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    return write


@pytest.fixture
def make_settings(write_config: Callable[[str], Path]) -> Callable[[str], Settings]:
    def make(text: str = "") -> Settings:
        return load_settings(write_config(text), env_file=None)

    return make


class Recorder(Notifier):
    """A notifier that keeps what it was asked to send."""

    def __init__(self, name: str = "console", ready: bool = True, fail: Exception | None = None) -> None:
        self.name = name  # type: ignore[misc]
        self.ready = ready
        self.fail = fail
        self.messages: list[Message] = []
        self.calls: list[dict[str, Any]] = []

    def configured(self) -> tuple[bool, str]:
        return self.ready, "test recorder" if self.ready else "set SOMETHING"

    def send(self, message: Message, *, job: str | None = None, run_id: str | None = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.messages.append(message)
        self.calls.append({"job": job, "run_id": run_id})


@pytest.fixture
def recorders() -> dict[str, Recorder]:
    return {name: Recorder(name) for name in ("console", "slack", "email")}


@pytest.fixture
def open_store(tmp_path: Path):
    """Open StateStores that are closed after the test (an unclosed SQLite connection is a warning)."""
    opened: list[StateStore] = []

    def open_(path: Path | str = "state.db") -> StateStore:
        store = StateStore(path if str(path) == ":memory:" else tmp_path / path)
        opened.append(store)
        return store

    yield open_
    for store in opened:
        store.close()


@pytest.fixture
def make_services(tmp_path: Path, recorders: dict[str, Recorder]) -> Callable[..., Services]:
    created: list[Services] = []

    def make(
        settings: Settings,
        *,
        registry: dict[str, type[Job]] | None = None,
        secrets: Secrets | None = None,
        offline: bool = False,
    ) -> Services:
        http = None if offline else HttpClient(settings.http, sleep=lambda _s: None)
        services = build_services(
            settings,
            secrets or Secrets(_env_file=None),
            offline=offline,
            registry=registry if registry is not None else discover_jobs(),
            store=StateStore(tmp_path / "state.db"),
            http=http,
            console_stream=io.StringIO(),
        )
        services.notifiers = dict(recorders)
        created.append(services)
        return services

    yield make
    for services in created:
        services.close()
