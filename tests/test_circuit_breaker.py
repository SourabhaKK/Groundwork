"""The breaker's contract, stated as tests.

Deterministic via an injected clock — no sleeps, so the cooldown behaviour is
asserted exactly rather than approximately.
"""

from __future__ import annotations

from src.llm.circuit_breaker import BreakerState, CircuitBreaker
from tests.conftest import FakeClock


def _breaker(clock: FakeClock, threshold: int = 3, cooldown: float = 60.0) -> CircuitBreaker:
    return CircuitBreaker(
        name="test", failure_threshold=threshold, cooldown_seconds=cooldown, clock=clock
    )


def test_starts_closed_and_allows_requests(clock: FakeClock) -> None:
    breaker = _breaker(clock)
    assert breaker.state is BreakerState.CLOSED
    assert breaker.allows_request()


def test_trips_only_on_the_nth_consecutive_failure(clock: FakeClock) -> None:
    breaker = _breaker(clock, threshold=3)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is BreakerState.CLOSED, "two failures must not trip a threshold of three"

    breaker.record_failure()
    assert breaker.state is BreakerState.OPEN
    assert not breaker.allows_request()


def test_success_resets_the_failure_count(clock: FakeClock) -> None:
    """A blip followed by recovery must not accumulate toward a trip."""
    breaker = _breaker(clock, threshold=3)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 2


def test_open_breaker_admits_exactly_one_probe_after_cooldown(clock: FakeClock) -> None:
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    breaker.record_failure()
    assert not breaker.allows_request()

    clock.advance(59.0)
    assert not breaker.allows_request(), "cooldown has not elapsed"

    clock.advance(1.0)
    assert breaker.allows_request(), "first caller after cooldown gets the probe"
    assert not breaker.allows_request(), "a concurrent caller must not also probe"


def test_successful_probe_closes_the_breaker(clock: FakeClock) -> None:
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    breaker.record_failure()
    clock.advance(60.0)

    assert breaker.allows_request()
    breaker.record_success()

    assert breaker.state is BreakerState.CLOSED
    assert breaker.allows_request()


def test_failed_probe_restarts_the_cooldown(clock: FakeClock) -> None:
    breaker = _breaker(clock, threshold=1, cooldown=60.0)
    breaker.record_failure()
    clock.advance(60.0)

    assert breaker.allows_request()
    breaker.record_failure()

    assert breaker.state is BreakerState.OPEN
    assert not breaker.allows_request()

    clock.advance(59.0)
    assert not breaker.allows_request(), "cooldown restarts from the failed probe"
    clock.advance(1.0)
    assert breaker.allows_request()


def test_snapshot_reports_state_for_health_endpoint(clock: FakeClock) -> None:
    breaker = _breaker(clock, threshold=1)
    breaker.record_failure()
    assert breaker.snapshot()["state"] == "open"
