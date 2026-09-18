"""Rate limiting and transient-error handling.

`request_delay=0` throughout, and every Retry-After in these tests is
0, so the suite exercises the retry paths without sleeping through
them.
"""

import time

import pytest
import requests

import tdapi
from tests.conftest import API_ROOT


def test_a_429_is_retried_and_the_eventual_200_is_returned(conn, requests_mock):
    route = requests_mock.get(
        API_ROOT + "ping",
        [
            {"status_code": 429, "headers": {"Retry-After": "0"}},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )
    assert conn.json_request(method="get", url_stem="ping") == {"ok": True}
    assert route.call_count == 2


@pytest.mark.parametrize("status", sorted(tdapi.RETRYABLE_STATUS_CODES))
def test_every_retryable_status_is_retried(conn, requests_mock, status):
    route = requests_mock.get(
        API_ROOT + "ping",
        [
            {"status_code": status, "headers": {"Retry-After": "0"}},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )
    conn.json_request(method="get", url_stem="ping")
    assert route.call_count == 2


def test_a_404_is_not_retried(conn, requests_mock):
    route = requests_mock.get(API_ROOT + "gone", status_code=404, text="nope")
    with pytest.raises(tdapi.TDException):
        conn.json_request(method="get", url_stem="gone")
    assert route.call_count == 1


def test_retries_are_bounded_and_then_the_real_failure_is_raised(conn, requests_mock):
    conn.max_attempts = 3
    route = requests_mock.get(
        API_ROOT + "boom", status_code=500, headers={"Retry-After": "0"}, text="kaboom"
    )
    with pytest.raises(tdapi.TDException) as excinfo:
        conn.json_request(method="get", url_stem="boom")
    assert route.call_count == 3
    # The caller sees the tenant's own failure, not a retry wrapper.
    assert "500" in str(excinfo.value)


def test_a_connection_error_is_retried_then_raises_naming_the_budget(conn, requests_mock):
    conn.max_attempts = 2
    requests_mock.get(API_ROOT + "down", exc=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(tdapi.TDException) as excinfo:
        conn.json_request(method="get", url_stem="down")
    assert "after 2 attempts" in str(excinfo.value)


def test_a_401_triggers_one_relogin_and_a_retry(conn, requests_mock):
    requests_mock.get(
        API_ROOT + "protected",
        [
            {"status_code": 401, "text": "expired"},
            {"status_code": 200, "json": {"ok": True}},
        ],
    )
    assert conn.json_request(method="get", url_stem="protected") == {"ok": True}


def test_rate_limit_headers_are_surfaced_to_the_caller(conn, requests_mock):
    requests_mock.get(
        API_ROOT + "ping",
        json={"ok": True},
        headers={
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "59",
            "X-RateLimit-Reset": "Wed, 21 Oct 2099 07:28:00 GMT",
        },
    )
    conn.json_request(method="get", url_stem="ping")
    assert conn.rate_limit_status["X-RateLimit-Limit"] == "60"
    assert conn.rate_limit_status["X-RateLimit-Remaining"] == "59"


def test_rate_limit_status_stays_empty_when_the_tenant_sends_nothing(conn, requests_mock):
    requests_mock.get(API_ROOT + "ping", json={"ok": True})
    conn.json_request(method="get", url_stem="ping")
    assert conn.rate_limit_status == {}


def test_an_unsupported_method_is_refused(conn):
    with pytest.raises(tdapi.TDException):
        conn.raw_request(method="options", url_stem="ping")


class TestParseRetryAfter:
    def test_delta_seconds(self):
        assert tdapi.parse_retry_after("30") == 30.0

    def test_an_http_date_becomes_a_delta(self):
        assert tdapi.parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT") > 0

    def test_a_past_date_is_clamped_to_zero(self):
        assert tdapi.parse_retry_after("Wed, 21 Oct 1999 07:28:00 GMT") == 0.0

    @pytest.mark.parametrize("value", [None, "", "   ", "soon"])
    def test_absent_or_unparseable_is_none_not_zero(self, value):
        # None means "the server gave us no delay", so the caller falls
        # back to its own backoff. Zero would mean "retry immediately".
        assert tdapi.parse_retry_after(value) is None


class TestRetryWait:
    def test_a_server_supplied_delay_wins(self, conn, requests_mock):
        requests_mock.get(API_ROOT + "x", status_code=429, headers={"Retry-After": "7"})
        resp = conn.session.get(API_ROOT + "x")
        assert conn.retry_wait(1, resp) == 7.0

    def test_an_absurd_server_delay_is_capped(self, conn, requests_mock):
        requests_mock.get(API_ROOT + "x", status_code=429, headers={"Retry-After": "99999"})
        resp = conn.session.get(API_ROOT + "x")
        assert conn.retry_wait(1, resp) == tdapi.DEFAULT_MAX_RETRY_WAIT

    def test_backoff_without_a_server_delay_is_jittered_within_the_ceiling(self, conn):
        waits = [conn.retry_wait(4, None) for _ in range(50)]
        ceiling = tdapi.DEFAULT_BACKOFF_BASE * (2**3)
        assert all(0 <= wait <= ceiling for wait in waits)
        # Full jitter, not a fixed schedule.
        assert len(set(waits)) > 1


class TestRateLimiter:
    def test_a_zero_interval_never_sleeps(self):
        limiter = tdapi.RateLimiter(0)
        started = time.monotonic()
        for _ in range(100):
            limiter.wait()
            limiter.record()
        assert time.monotonic() - started < 0.1

    def test_the_interval_is_enforced_between_recorded_requests(self):
        limiter = tdapi.RateLimiter(0.05)
        started = time.monotonic()
        for _ in range(3):
            limiter.wait()
            limiter.record()
        # Three slots, two of which are waited out.
        assert time.monotonic() - started >= 0.1

    def test_pause_holds_the_next_request_off(self):
        limiter = tdapi.RateLimiter(0)
        limiter.pause(0.05)
        started = time.monotonic()
        limiter.wait()
        assert time.monotonic() - started >= 0.04

    @pytest.mark.parametrize("seconds", [None, 0, -5])
    def test_a_non_positive_pause_is_a_no_op(self, seconds):
        limiter = tdapi.RateLimiter(0)
        limiter.pause(seconds)
        started = time.monotonic()
        limiter.wait()
        assert time.monotonic() - started < 0.05
