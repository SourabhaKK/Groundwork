"""Per-source failure handling (Phase 2.4).

Companies House and Tavily each get their own instance of the Phase 0.6 state
machine — 3 consecutive transient failures, 60s cooldown, one half-open probe —
with **no fallback target**. That absence is the whole difference from the LLM
router: when Anthropic is down there is Gemini, but there is no second Companies
House. An open breaker here means the call fails immediately and the report says
so, which is the correct outcome, not a degraded one.

**One breaker per provider, not per endpoint.** What goes down is the host, not
the URL path. With a breaker each on filing history, officers and PSC, a dead
register would need nine consecutive failures to stop being called instead of
three, and every report in between would pay three timeouts to rediscover the
same outage. Per-provider state is also the only way one report's failures can
protect the next one.

**What may trip a breaker.** Only genuinely transient failures — 5xx, 429,
timeouts, connection errors. A 404 (including the ordinary "no PSC recorded")
and our own malformed-request 4xx never do. This is Phase 0.6's classification
applied to a second kind of provider, for the same reason: a breaker that trips
on our own bug stops us calling a perfectly healthy register and sends debugging
at the wrong system entirely.

**Tavily's quota exhaustion does trip its breaker**, and that is a judgement
call rather than an obvious reading. Strictly, being out of credits is our
budget problem, not Tavily's outage. It is included because once credits are
gone, every later report otherwise fires three doomed searches and waits on
three rejections before printing the same gap it could have printed instantly.
The 60-second probe is also exactly right for the ambiguity noted in
`tavily.py`: a 429 may be short-term rate limiting rather than an exhausted
plan, and if it was, the probe recovers automatically without special-casing.
A probe costs one rejected HTTP request, not a credit.

**Known limitation, same as the rate limiter's:** this state is in-process. It
resets when Cloud Run replaces the container and would be per-instance if the
service scaled out — bounded by pinning `--max-instances=2` at deploy and
disclosed in the README rather than presented as airtight.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

from src.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.config import get_settings
from src.data_sources.companies_house import CompaniesHouseUnavailable
from src.data_sources.tavily import TavilyQuotaExhausted, TavilyUnavailable

logger = logging.getLogger(__name__)

T = TypeVar("T")

COMPANIES_HOUSE = "companies_house"
TAVILY = "tavily"

_breakers: dict[str, CircuitBreaker] = {}


def get_breaker(name: str) -> CircuitBreaker:
    """The process-wide breaker for one source, created on first use.

    Process-wide on purpose: a breaker whose state died with each report would
    only ever protect calls inside a single run, which is the one place the
    concurrency already makes it nearly useless — all three of a gather's calls
    are in flight before any of them can fail.
    """
    breaker = _breakers.get(name)
    if breaker is None:
        settings = get_settings()
        breaker = CircuitBreaker(
            name=name,
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        )
        _breakers[name] = breaker
    return breaker


def reset_breakers() -> None:
    """Test hook — drops every cached breaker so state can't leak across cases."""
    _breakers.clear()


def breaker_snapshots() -> list[dict[str, object]]:
    """Diagnostic view for /health.

    Only reports breakers that exist. An empty list means no source has been
    called yet this container lifetime, which is different from — and must not
    be rendered as — every source being healthy.
    """
    return [breaker.snapshot() for breaker in _breakers.values()]


def is_companies_house_transient(error: BaseException) -> bool:
    """The register's own three-way split, already made in `companies_house.py`.

    `CompaniesHouseUnavailable` is the only transient one. `CompanyNotFound` is
    a reportable fact and `CompaniesHouseAuthError` is our misconfiguration —
    neither says anything about whether the register is up.
    """
    return isinstance(error, CompaniesHouseUnavailable)


def is_tavily_transient(error: BaseException) -> bool:
    """Unavailability, plus exhaustion — see this module's docstring for why the
    second one is included despite not being Tavily's fault."""
    return isinstance(error, TavilyUnavailable | TavilyQuotaExhausted)


async def guarded(
    breaker: CircuitBreaker,
    call: Callable[[], Awaitable[T]],
    *,
    is_transient: Callable[[BaseException], bool],
    what: str = "",
) -> T:
    """Run one source call under a breaker.

    Takes a factory rather than an awaitable so nothing is scheduled when the
    breaker is open — awaiting a coroutine that is never created is the point of
    failing fast.

    Three outcomes, deliberately distinct:

    - **Breaker open** — raises `CircuitOpenError` without calling out. The
      caller records it as a gap; the report states it.
    - **Transient failure** — recorded against the breaker, then re-raised. The
      caller still states the gap; the breaker remembers for next time.
    - **Non-transient failure** — re-raised with the breaker untouched. Our bug,
      or a fact about the company. Neither is evidence the provider is down.
    """
    if not breaker.allows_request():
        raise CircuitOpenError(
            f"{breaker.name} circuit is open{f' — skipped {what}' if what else ''}; not calling out"
        )

    try:
        result = await call()
    except BaseException as exc:
        if is_transient(exc):
            breaker.record_failure()
            logger.warning(
                "source %s: transient failure (%s); breaker=%s",
                breaker.name,
                type(exc).__name__,
                breaker.state.value,
            )
        else:
            logger.debug(
                "source %s: non-transient %s, breaker untouched",
                breaker.name,
                type(exc).__name__,
            )
        raise

    breaker.record_success()
    return result
