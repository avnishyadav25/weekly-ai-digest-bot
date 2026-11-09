"""Logging with the standard library: one JSON object per line, with the job name and run id on every line.

Usage in a job: ``ctx.log.info("checked url", extra={"url": url, "status": 200})``. Every key passed in ``extra``
becomes a field of the JSON line. The runner wraps each run in :func:`log_context`, so ``job`` and ``run_id`` are
added for you, including to log lines from libraries.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

_context: ContextVar[dict[str, Any] | None] = ContextVar("automation_log_context", default=None)

# Attributes every LogRecord has; anything else on a record came from ``extra=`` or the context.
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime", "taskName"}
_HANDLER_NAME = "automation"
# Libraries that log every scheduler tick at INFO: quiet unless the level is DEBUG.
_QUIET_LOGGERS = ("apscheduler", "tzlocal")
# httpx and httpcore log full request URLs, which can carry secrets (a Slack webhook URL, a token in a query
# string), so they stay at WARNING even at DEBUG. automation.http logs each request at DEBUG without them.
_URL_LOGGERS = ("httpx", "httpcore")


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Add fields (for example ``job`` and ``run_id``) to every log line written inside the block."""
    token = _context.set({**(_context.get() or {}), **fields})
    try:
        yield
    finally:
        _context.reset(token)


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for key, value in (_context.get() or {}).items():
            if not hasattr(record, key):
                setattr(record, key, value)
        return True


def _timestamp(record: logging.LogRecord) -> str:
    return (
        datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {k: v for k, v in record.__dict__.items() if k not in _RESERVED and not k.startswith("_")}


class JsonFormatter(logging.Formatter):
    """``{"ts": ..., "level": ..., "logger": ..., "event": ..., "job": ..., "run_id": ..., ...extra}``."""

    def format(self, record: logging.LogRecord) -> str:
        line: dict[str, Any] = {
            "ts": _timestamp(record),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        extras = _extras(record)
        for key in ("job", "run_id"):
            if key in extras:
                line[key] = extras.pop(key)
        line.update(extras)
        if record.exc_info:
            line["exc"] = self.formatException(record.exc_info)
        return json.dumps(line, default=str, ensure_ascii=False)


class TextFormatter(logging.Formatter):
    """Readable lines for a terminal: ``2026-10-01T07:00:00.000Z INFO uptime-check checked url=... status=200``."""

    def format(self, record: logging.LogRecord) -> str:
        extras = _extras(record)
        job = extras.pop("job", None)
        run_id = extras.pop("run_id", None)
        parts = [_timestamp(record), f"{record.levelname:<7}"]
        if job:
            parts.append(f"{job}[{run_id}]" if run_id else str(job))
        parts.append(record.getMessage())
        parts.extend(f"{k}={v}" for k, v in extras.items())
        text = " ".join(parts)
        if record.exc_info:
            text += "\n" + self.formatException(record.exc_info)
        return text


def setup_logging(level: str = "INFO", fmt: str = "json", stream: TextIO | None = None) -> logging.Handler:
    """Send all logging to ``stream`` (default stderr) in the chosen format. Safe to call more than once."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == _HANDLER_NAME:
            root.removeHandler(handler)
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.set_name(_HANDLER_NAME)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(ContextFilter())
    root.addHandler(handler)
    root.setLevel(level.upper())
    quiet = logging.DEBUG if level.upper() == "DEBUG" else logging.WARNING
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(quiet)
    for name in _URL_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    return handler


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"automation.{name}")
