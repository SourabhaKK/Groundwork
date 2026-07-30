"""Two limits on /report, for two different threats.

Per-IP sliding window: stops one client looping the endpoint.
Global budget: the backstop if the shared token leaks.

Both counters live in this process. That is a real limitation, stated plainly
rather than papered over:

  * The counters reset when Cloud Run replaces the container. So the global
    budget is really "200 per container lifetime", not an all-time guarantee.
    This is the *likely* failure, not the exotic one.
  * If Cloud Run ever scales past one instance, each gets its own counters and
    the effective cap multiplies. Deployment pins --max-instances=2 to bound
    that; see README.

The durable ceiling is upstream anyway: Tavily's free tier stops at 1,000
credits (~250-400 runs) on its own, and provider-side spend limits cap the
rest. This limiter is a courteous speed bump, not a budget guarantee.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status

from src.config import Settings, get_settings

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 3600.0


class RateLimiter:
    def __init__(
        self,
        per_ip_per_hour: int,
        global_budget: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.per_ip_per_hour = per_ip_per_hour
        self.global_budget = global_budget
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._global_used = 0

    @property
    def global_used(self) -> int:
        return self._global_used

    @property
    def global_remaining(self) -> int:
        return max(0, self.global_budget - self._global_used)

    def check_and_consume(self, client_ip: str) -> None:
        """Raise 429 if either limit is exhausted; otherwise record the hit."""
        if self._global_used >= self.global_budget:
            logger.warning("global report budget exhausted (%d)", self.global_budget)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Global demo budget of {self.global_budget} reports is exhausted. "
                    "This cap exists because the demo runs on a personal API key and a "
                    "metered search tier — see the README. Contact the author to reset it."
                ),
            )

        now = self._clock()
        window = self._hits[client_ip]
        while window and (now - window[0]) >= _WINDOW_SECONDS:
            window.popleft()

        if len(window) >= self.per_ip_per_hour:
            retry_after = int(_WINDOW_SECONDS - (now - window[0])) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=(
                    f"Rate limit: {self.per_ip_per_hour} reports per hour per client. "
                    f"Retry in {retry_after}s."
                ),
                headers={"Retry-After": str(retry_after)},
            )

        window.append(now)
        self._global_used += 1

    def snapshot(self) -> dict[str, int]:
        return {
            "global_budget": self.global_budget,
            "global_used": self._global_used,
            "global_remaining": self.global_remaining,
            "per_ip_per_hour": self.per_ip_per_hour,
        }


_limiter: RateLimiter | None = None


def get_limiter(settings: Settings = Depends(get_settings)) -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter(
            per_ip_per_hour=settings.per_ip_reports_per_hour,
            global_budget=settings.global_report_budget,
        )
    return _limiter


def reset_limiter() -> None:
    """Test hook."""
    global _limiter
    _limiter = None


def _client_ip(request: Request) -> str:
    # Cloud Run terminates TLS upstream, so the direct peer is a proxy.
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(
    request: Request,
    limiter: RateLimiter = Depends(get_limiter),
) -> None:
    limiter.check_and_consume(_client_ip(request))
