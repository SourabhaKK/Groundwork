"""Rate limiter unit tests, with an injected clock so the hour window is exact."""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api.rate_limit import RateLimiter
from tests.conftest import FakeClock


def test_allows_up_to_the_per_ip_limit(clock: FakeClock) -> None:
    limiter = RateLimiter(per_ip_per_hour=3, global_budget=100, clock=clock)

    for _ in range(3):
        limiter.check_and_consume("1.2.3.4")

    with pytest.raises(HTTPException) as exc:
        limiter.check_and_consume("1.2.3.4")
    assert exc.value.status_code == 429


def test_per_ip_limit_is_per_client(clock: FakeClock) -> None:
    limiter = RateLimiter(per_ip_per_hour=1, global_budget=100, clock=clock)

    limiter.check_and_consume("1.2.3.4")
    limiter.check_and_consume("5.6.7.8")  # a different evaluator is unaffected

    with pytest.raises(HTTPException):
        limiter.check_and_consume("1.2.3.4")


def test_window_slides_so_a_blocked_client_recovers(clock: FakeClock) -> None:
    limiter = RateLimiter(per_ip_per_hour=1, global_budget=100, clock=clock)
    limiter.check_and_consume("1.2.3.4")

    clock.advance(3599.0)
    with pytest.raises(HTTPException):
        limiter.check_and_consume("1.2.3.4")

    clock.advance(1.0)
    limiter.check_and_consume("1.2.3.4")  # window has rolled off


def test_retry_after_header_is_set(clock: FakeClock) -> None:
    limiter = RateLimiter(per_ip_per_hour=1, global_budget=100, clock=clock)
    limiter.check_and_consume("1.2.3.4")
    clock.advance(600.0)

    with pytest.raises(HTTPException) as exc:
        limiter.check_and_consume("1.2.3.4")

    assert exc.value.headers is not None
    assert int(exc.value.headers["Retry-After"]) == pytest.approx(3001, abs=2)


def test_global_budget_stops_everyone(clock: FakeClock) -> None:
    """The backstop if the shared token leaks: distinct IPs don't get around it."""
    limiter = RateLimiter(per_ip_per_hour=100, global_budget=2, clock=clock)

    limiter.check_and_consume("1.1.1.1")
    limiter.check_and_consume("2.2.2.2")

    with pytest.raises(HTTPException) as exc:
        limiter.check_and_consume("3.3.3.3")

    assert exc.value.status_code == 429
    assert "budget" in exc.value.detail.lower()


def test_global_budget_is_not_refunded_by_the_sliding_window(clock: FakeClock) -> None:
    """The global cap is a total for the review window, not a per-hour allowance."""
    limiter = RateLimiter(per_ip_per_hour=100, global_budget=1, clock=clock)
    limiter.check_and_consume("1.1.1.1")

    clock.advance(86_400.0)

    with pytest.raises(HTTPException):
        limiter.check_and_consume("1.1.1.1")


def test_snapshot_reports_remaining_budget(clock: FakeClock) -> None:
    limiter = RateLimiter(per_ip_per_hour=5, global_budget=10, clock=clock)
    limiter.check_and_consume("1.1.1.1")

    assert limiter.snapshot() == {
        "global_budget": 10,
        "global_used": 1,
        "global_remaining": 9,
        "per_ip_per_hour": 5,
    }
