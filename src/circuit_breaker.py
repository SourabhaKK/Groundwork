"""A stateful circuit breaker, shared by every external provider.

The state is the whole point. A stateless try/except fallback re-attempts a
provider that is known to be down on *every* call, so a real outage adds the
full timeout to every request — on a pipeline with ~10+ LLM calls per report,
that is minutes of dead wait. Remembering that the provider is down is what
turns "handle the error" into "stop paying for the error".

Lives at `src/` root rather than inside `src/llm/`, where it started. Phase 2.4
gives Companies House and Tavily their own instances of this same state
machine, and a data-source module reaching into the LLM package for a generic
mechanism would misrepresent what this is. Note what is shared and what is not:
each consumer instantiates its own breaker with its own state, and the LLM
router's breaker — which has a fallback provider to route to — is never reused
for sources that have no alternate provider to switch to.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from enum import StrEnum

logger = logging.getLogger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when the breaker refuses a call because the provider is cooling down."""


class CircuitBreaker:
    """Trips after N consecutive transient failures, then probes once per cooldown.

    Only *transient* failures reach `record_failure` — the caller classifies.
    A 400 caused by our own malformed prompt must not look like a provider
    outage, or a bug in our schema silently demotes every later call to the
    fallback model and we end up debugging the wrong system.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 3,
        cooldown_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float | None = None
        self._probe_in_flight = False

    @property
    def state(self) -> BreakerState:
        """Current state, resolving an elapsed cooldown to HALF_OPEN lazily."""
        if self._state is BreakerState.OPEN and self._cooldown_elapsed():
            return BreakerState.HALF_OPEN
        return self._state

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _cooldown_elapsed(self) -> bool:
        if self._opened_at is None:
            return False
        return (self._clock() - self._opened_at) >= self.cooldown_seconds

    def allows_request(self) -> bool:
        """True if a call may proceed. Admits exactly one probe per cooldown."""
        if self._state is BreakerState.CLOSED:
            return True

        if self._state is BreakerState.OPEN:
            if not self._cooldown_elapsed():
                return False
            # Cooldown served: promote to half-open and let this caller probe.
            self._state = BreakerState.HALF_OPEN
            self._probe_in_flight = True
            logger.info("breaker %s: cooldown elapsed, probing primary", self.name)
            return True

        # HALF_OPEN: one probe at a time, so a burst of concurrent requests
        # doesn't all slam a provider we only suspect has recovered.
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        if self._state is not BreakerState.CLOSED:
            logger.info("breaker %s: closing after successful call", self.name)
        self._state = BreakerState.CLOSED
        self._consecutive_failures = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False

        if self._state is BreakerState.HALF_OPEN:
            # The probe failed — still down. Restart the cooldown.
            self._trip("probe failed")
            return

        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._trip(f"{self._consecutive_failures} consecutive transient failures")

    def _trip(self, reason: str) -> None:
        self._state = BreakerState.OPEN
        self._opened_at = self._clock()
        logger.warning("breaker %s: OPEN for %.0fs (%s)", self.name, self.cooldown_seconds, reason)

    def snapshot(self) -> dict[str, object]:
        """Diagnostic view, surfaced on /health."""
        return {
            "name": self.name,
            "state": self.state.value,
            "consecutive_failures": self._consecutive_failures,
        }
