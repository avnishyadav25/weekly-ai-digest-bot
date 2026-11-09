from __future__ import annotations

import io
import json
import logging

from automation.logging import get_logger, log_context, setup_logging


def lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_line_per_event_with_job_and_run_id():
    stream = io.StringIO()
    setup_logging("INFO", "json", stream)
    log = get_logger("jobs.demo")
    with log_context(job="demo", run_id="abc123"):
        log.info("checked url", extra={"url": "https://example.com/", "status": 200})
    log.info("outside")
    first, second = lines(stream)
    assert first["event"] == "checked url"
    assert first["level"] == "info"
    assert first["logger"] == "automation.jobs.demo"
    assert first["job"] == "demo"
    assert first["run_id"] == "abc123"
    assert first["url"] == "https://example.com/"
    assert first["status"] == 200
    assert first["ts"].endswith("Z")
    assert "job" not in second


def test_context_reaches_library_loggers_and_nests():
    stream = io.StringIO()
    setup_logging("INFO", "json", stream)
    with log_context(job="outer", run_id="r1"), log_context(step="two"):
        logging.getLogger("somelib").warning("from a library")
    (line,) = lines(stream)
    assert (line["job"], line["run_id"], line["step"]) == ("outer", "r1", "two")


def test_exceptions_are_kept_in_one_line():
    stream = io.StringIO()
    setup_logging("INFO", "json", stream)
    try:
        raise ValueError("boom")
    except ValueError:
        get_logger("x").exception("failed")
    (line,) = lines(stream)
    assert "ValueError: boom" in line["exc"]
    assert len(stream.getvalue().strip().splitlines()) == 1


def test_text_format_is_readable():
    stream = io.StringIO()
    setup_logging("INFO", "text", stream)
    with log_context(job="demo", run_id="r1"):
        get_logger("x").info("hello", extra={"n": 3})
    text = stream.getvalue()
    assert "INFO" in text
    assert "demo[r1] hello n=3" in text


def test_setup_is_idempotent_and_respects_the_level():
    stream = io.StringIO()
    setup_logging("WARNING", "json", stream)
    setup_logging("WARNING", "json", stream)
    get_logger("x").info("hidden")
    get_logger("x").warning("shown")
    assert [line["event"] for line in lines(stream)] == ["shown"]
    named = [h for h in logging.getLogger().handlers if h.get_name() == "automation"]
    assert len(named) == 1


def test_noisy_libraries_are_quiet_unless_debug():
    setup_logging("INFO", "json", io.StringIO())
    assert logging.getLogger("apscheduler").level == logging.WARNING
    setup_logging("DEBUG", "json", io.StringIO())
    assert logging.getLogger("apscheduler").level == logging.DEBUG


def test_httpx_never_logs_request_urls():
    setup_logging("DEBUG", "json", io.StringIO())
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING
