from __future__ import annotations

from datetime import timedelta

import httpx
import pytest
import respx

from automation.runner import run_job
from tests.conftest import NOW

CONFIG = """
[jobs.uptime-check]
urls = ["https://a.example.com/", "https://b.example.com/health"]
retries = 0
"""


@pytest.fixture
def services(make_settings, make_services):
    return make_services(make_settings(CONFIG))


def run(services, minutes=0, **kwargs):
    return run_job(services, "uptime-check", now=NOW + timedelta(minutes=minutes), **kwargs)


@respx.mock
def test_alerts_once_when_a_url_goes_down_then_once_when_it_recovers(services, recorders):
    respx.get("https://a.example.com/").mock(return_value=httpx.Response(200))
    b = respx.get("https://b.example.com/health")

    b.mock(return_value=httpx.Response(200))
    assert run(services).summary == "2 checked: 2 up, 0 down; 0 changes alerted"
    assert recorders["console"].messages == []  # up on the first check: nothing to say

    b.mock(return_value=httpx.Response(503))
    outcome = run(services, 5)
    assert outcome.status == "success"  # a down URL is a result, not a job failure
    (alert,) = recorders["console"].messages
    assert alert.title == "uptime-check: 1 URL down"
    assert alert.level == "error"
    assert "https://b.example.com/health is down: HTTP 503" in alert.text

    run(services, 10)  # still down: no second alert
    assert len(recorders["console"].messages) == 1

    b.mock(return_value=httpx.Response(200))
    run(services, 35)
    recovered = recorders["console"].messages[-1]
    assert recovered.title == "uptime-check: 1 URL back up"
    assert recovered.level == "info"
    assert "(down for 30 min)" in recovered.text


@respx.mock
def test_down_on_the_first_check_alerts_unless_turned_off(make_settings, make_services, recorders):
    respx.get("https://a.example.com/").mock(return_value=httpx.Response(200))
    respx.get("https://b.example.com/health").mock(side_effect=httpx.ConnectError("refused"))
    services = make_services(make_settings(CONFIG + "alert_on_first_check = false\n"))
    run(services)
    assert recorders["console"].messages == []
    services.settings.jobs["uptime-check"]["alert_on_first_check"] = True
    services.store.clear("uptime-check")
    run(services, 1)
    assert "ConnectError: refused" in recorders["console"].messages[0].text


@respx.mock
def test_status_and_text_expectations(make_settings, make_services, recorders):
    respx.get("https://a.example.com/").mock(return_value=httpx.Response(200, text="maintenance page"))
    respx.get("https://b.example.com/health").mock(return_value=httpx.Response(204))
    services = make_services(make_settings(CONFIG + 'contains = "Welcome"\nexpect_status = [200]\n'))
    run(services)
    text = recorders["console"].messages[0].text
    assert "a.example.com/ is down: HTTP 200 but the text 'Welcome' is missing" in text
    assert "b.example.com/health is down: HTTP 204" in text
    assert recorders["console"].messages[0].title == "uptime-check: 2 URLs down"


@respx.mock
def test_quick_retry_avoids_a_false_alarm(make_settings, make_services, recorders):
    respx.get("https://a.example.com/").mock(return_value=httpx.Response(200))
    respx.get("https://b.example.com/health").mock(side_effect=[httpx.Response(502), httpx.Response(200)])
    services = make_services(make_settings(CONFIG.replace("retries = 0", "retries = 1")))
    assert run(services).data["down"] == 0


@respx.mock
def test_failed_alert_is_retried_on_the_next_run(services, recorders):
    respx.get("https://a.example.com/").mock(return_value=httpx.Response(200))
    respx.get("https://b.example.com/health").mock(return_value=httpx.Response(500))
    recorders["console"].fail = RuntimeError("console broken")
    outcome = run(services)
    assert outcome.status == "failed"
    assert services.store.get_value("uptime-check", "status:https://b.example.com/health") is None
    recorders["console"].fail = None
    run(services, 5)
    assert [m.title for m in recorders["console"].messages] == ["uptime-check: 1 URL down"]


@respx.mock
def test_dry_run_plans_the_alert_and_saves_nothing(services, recorders):
    respx.get("https://a.example.com/").mock(return_value=httpx.Response(200))
    respx.get("https://b.example.com/health").mock(return_value=httpx.Response(503))
    outcome = run(services, dry_run=True)
    assert recorders["console"].messages == []
    assert [p.message.title for p in outcome.planned_notifications] == ["uptime-check: 1 URL down"]
    assert any(item.startswith("set status:https://b.example.com/health") for item in outcome.planned_state)
    assert services.store.values("uptime-check") == {}
    assert services.store.runs() == []
