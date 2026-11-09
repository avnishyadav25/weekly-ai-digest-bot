from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from automation.jobs.csv_report import period_key
from automation.runner import run_job
from tests.conftest import NOW

CSV = """date,region,units,revenue
2026-09-21,North,4,120.00
2026-09-21,South,1,45.50
2026-09-22,North,2,"1,091.00"
2026-09-23,East,3,
"""


def config(source: str, extra: str = "") -> str:
    return (
        f'[jobs.csv-report]\nsource = "{source}"\ngroup_by = "region"\nsum = ["units", "revenue"]\n'
        f'title = "Orders"\n{extra}'
    )


def test_period_keys():
    assert period_key(NOW, "day") == "2026-10-01"
    assert period_key(NOW, "week") == "2026-W40"
    assert period_key(NOW, "month") == "2026-10"
    assert period_key(NOW, "run") is None


def test_groups_sums_and_totals_from_a_file(tmp_path, make_settings, make_services, recorders):
    (tmp_path / "orders.csv").write_text(CSV, encoding="utf-8")
    services = make_services(make_settings(config("orders.csv")))
    outcome = run_job(services, "csv-report", now=NOW)
    assert outcome.summary == "report sent: 4 rows in 3 groups"
    message = recorders["console"].messages[0]
    assert message.title == "Orders (2026-W40)"
    lines = message.text.splitlines()
    assert lines[0] == "4 rows from orders.csv (period 2026-W40)"
    assert lines[2].split() == ["region", "rows", "units", "revenue"]
    assert lines[3].split() == ["North", "2", "6", "1,211.00"]  # sorted by the first sum column
    assert lines[-1].split() == ["Total", "4", "10", "1,256.50"]
    assert "<th" in message.html
    assert "<b>1,256.50</b>" in message.html


@respx.mock
def test_reads_a_url_and_sends_once_per_week(make_settings, make_services, recorders):
    url = "https://example.com/export.csv"
    respx.get(url).mock(return_value=httpx.Response(200, text=CSV))
    services = make_services(make_settings(config(url)))
    run_job(services, "csv-report", now=NOW)
    second = run_job(services, "csv-report", now=NOW + timedelta(days=1))
    assert second.summary == "report for 2026-W40 already sent; skipped"
    next_week = run_job(services, "csv-report", now=NOW + timedelta(days=7))
    assert next_week.summary.startswith("report sent")
    assert len(recorders["console"].messages) == 2


@respx.mock
def test_http_errors_are_reported_without_the_query_string(make_settings, make_services, recorders):
    url = "https://example.com/export.csv?token=abc123"
    respx.get(url).mock(return_value=httpx.Response(500))
    outcome = run_job(make_services(make_settings(config(url, 'once_per = "run"\n'))), "csv-report", now=NOW)
    assert outcome.error == "HTTP 500 from https://example.com/export.csv"
    assert "abc123" not in recorders["console"].messages[-1].text


def test_once_per_run_sends_every_time(tmp_path, make_settings, make_services, recorders):
    (tmp_path / "orders.csv").write_text(CSV, encoding="utf-8")
    services = make_services(make_settings(config("orders.csv", 'once_per = "run"\n')))
    run_job(services, "csv-report", now=NOW)
    run_job(services, "csv-report", now=NOW)
    assert len(recorders["console"].messages) == 2
    assert recorders["console"].messages[0].title == "Orders"


@pytest.mark.parametrize(
    ("csv_text", "error"),
    [
        (
            "date,area,units,revenue\n2026-09-21,North,1,2\n",
            "column(s) region not in the CSV header (date, area, units, revenue)",
        ),
        ("date,region,units,revenue\n2026-09-21,North,many,2\n", "line 2: units = 'many' is not a number"),
    ],
)
def test_bad_input_fails_with_a_clear_message(
    tmp_path, make_settings, make_services, recorders, csv_text, error
):
    (tmp_path / "orders.csv").write_text(csv_text, encoding="utf-8")
    outcome = run_job(make_services(make_settings(config("orders.csv"))), "csv-report", now=NOW)
    assert outcome.status == "failed"
    assert outcome.error == error
    # the run failure alert went to the on_failure channel instead
    assert recorders["console"].messages[-1].title == "csv-report failed"


def test_missing_file(make_settings, make_services):
    outcome = run_job(make_services(make_settings(config("nope.csv"))), "csv-report", now=NOW)
    assert outcome.error == "CSV file not found: nope.csv"


def test_semicolon_files_and_a_byte_order_mark(tmp_path, make_settings, make_services, recorders):
    (tmp_path / "orders.csv").write_text("﻿region;units;revenue\nNorth;1;2\n", encoding="utf-8")
    services = make_services(make_settings(config("orders.csv", 'delimiter = ";"\n')))
    assert run_job(services, "csv-report", now=NOW).status == "success"
