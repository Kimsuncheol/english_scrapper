"""External scheduler cold-start, authentication, retry and restart behavior."""

from datetime import datetime, timezone
from io import BytesIO
import json
from urllib.error import HTTPError, URLError

import pytest

from scripts.trigger_daily import DailyClient, NoRedirect, run_daily

BASE = "https://lesson-service.onrender.com"
TOKEN = "test-secret-never-log"
NOW = datetime(2026, 10, 21, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.value = 0
        self.delays = []

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.delays.append(seconds)
        self.value += seconds


class Opener:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return BytesIO(reply if isinstance(reply, bytes) else json.dumps(reply).encode())


def client(replies, budget=1800):
    clock = Clock()
    opener = Opener(replies)
    return DailyClient(BASE, TOKEN, opener=opener, sleep=clock.sleep,
        monotonic=clock, budget_seconds=budget), opener, clock


def status(state=None, **kwargs):
    return {"enabled": True, "timezone": "Asia/Seoul", "hour": 6, "minute": 0,
        "last_run": {"scheduled_date": "2026-10-21", "status": state} if state else None,
        **kwargs}


def error(code):
    return HTTPError(BASE, code, TOKEN, {}, BytesIO(TOKEN.encode()))


@pytest.mark.parametrize("code", [502, 503, 504])
def test_cold_start_and_network_errors_are_retried_with_bounded_backoff(code):
    api, opener, clock = client([error(code), b"<html>Waking up</html>", URLError(TOKEN), status()])
    assert api.request("/api/v1/jobs/status")["enabled"]
    assert clock.delays == [1, 2, 4]
    assert len(opener.calls) == 4
    assert all(timeout <= 60 for _, timeout in opener.calls)


def test_retry_exhaustion_does_not_print_secret_or_response_body(capsys):
    api, opener, clock = client([URLError(TOKEN) for _ in range(6)])
    with pytest.raises(RuntimeError) as failure:
        api.request("/api/v1/jobs/status")
    assert TOKEN not in str(failure.value)
    assert TOKEN not in capsys.readouterr().out
    assert len(opener.calls) == 6
    assert clock.delays == [1, 2, 4, 8, 16]


@pytest.mark.parametrize("code", [301, 302, 307, 308, 401, 403, 404])
def test_auth_and_redirect_failures_do_not_retry_or_forward_credentials(code):
    api, opener, clock = client([error(code)])
    with pytest.raises(RuntimeError, match=f"HTTP {code}") as failure:
        api.request("/api/v1/jobs/daily/trigger", post=True)
    assert TOKEN not in str(failure.value)
    assert len(opener.calls) == 1 and not clock.delays
    request, _ = opener.calls[0]
    assert request.full_url == BASE + "/api/v1/jobs/daily/trigger"
    assert NoRedirect().redirect_request(request, None, code, "", {}, "https://evil.test") is None


def test_restart_recovery_retriggers_until_persisted_completion(capsys):
    api, opener, clock = client([
        status(), {"status": "accepted"},
        status("running"), {"status": "already_running"},
        status("interrupted"), {"status": "accepted"},
        status("completed"),
    ])
    run_daily(api, now=NOW)
    assert clock.delays == [30, 30, 30]
    assert [request.get_method() for request, _ in opener.calls] == ["GET", "POST", "GET", "POST", "GET", "POST", "GET"]
    for request, _ in opener.calls:
        expected = "Bearer " + TOKEN if request.get_method() == "POST" else None
        assert request.get_header("Authorization") == expected
        assert TOKEN not in request.full_url
    assert TOKEN not in capsys.readouterr().out


def test_already_completed_job_does_not_trigger_another_run():
    api, opener, clock = client([status("completed")])
    run_daily(api, now=NOW)
    assert len(opener.calls) == 1 and not clock.delays


@pytest.mark.parametrize("state", ["failed", "partial"])
def test_recorded_failure_is_visible_to_external_scheduler(state):
    api, opener, _ = client([status(state)])
    with pytest.raises(RuntimeError, match="recorded failures"):
        run_daily(api, now=NOW)
    assert len(opener.calls) == 1


def test_restart_polling_has_total_budget_and_cooldown_is_retried():
    api, opener, clock = client([
        status(local_error_code="storage_unavailable"), {"status": "cooldown"},
        status("interrupted"), {"status": "accepted"},
        status("running"), {"status": "already_running"},
    ], budget=65)
    with pytest.raises(RuntimeError, match="budget"):
        run_daily(api, now=NOW)
    assert clock.value == 65 and clock.delays == [30, 30, 5]
    assert len(opener.calls) == 6


def test_http_wake_retries_share_the_overall_budget():
    api, opener, clock = client([URLError(TOKEN)] * 6, budget=5)
    with pytest.raises(RuntimeError, match="budget"):
        api.request("/api/v1/jobs/status")
    assert clock.value == 5
    assert len(opener.calls) == 3
    assert all(timeout <= 5 for _, timeout in opener.calls)


@pytest.mark.parametrize("value", [status(enabled=False), status(hour=23)])
def test_disabled_or_early_schedule_is_a_configuration_failure(value):
    api, opener, _ = client([value])
    with pytest.raises(RuntimeError):
        run_daily(api, now=NOW)
    assert len(opener.calls) == 1


@pytest.mark.parametrize("base", ["http://service.test", "https://user:secret@service.test", "https://service.test/path", "https://service.test?token=x"])
def test_invalid_service_origin_rejected(base):
    with pytest.raises(ValueError, match="HTTPS service origin"):
        DailyClient(base, TOKEN)


@pytest.mark.parametrize("token", ["", "secret\nvalue", "secret value", "비밀"])
def test_invalid_secret_is_rejected_without_echoing_it(token):
    with pytest.raises(ValueError) as failure:
        DailyClient(BASE, token)
    if token:
        assert token not in str(failure.value)


def test_oversized_api_response_is_rejected():
    api, opener, _ = client([b"x" * 1_048_577] * 6)
    with pytest.raises(RuntimeError, match="did not become available"):
        api.request("/api/v1/jobs/status")
    assert len(opener.calls) == 6
