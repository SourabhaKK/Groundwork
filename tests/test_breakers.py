"""Per-source circuit breakers (Phase 2.4).

The state machine itself is already pinned by `test_circuit_breaker.py`. What
these tests cover is the part that is specific to sources: which failures are
allowed to trip a breaker, that an open one produces a stated gap rather than a
crash or a silence, and that the two sources cannot affect each other.

The last point matters more than it looks. If a Tavily outage tripped the
Companies House breaker, a web-search failure would suppress the register — the
strongest evidence in the system — and the report would render thin for a reason
that had nothing to do with the register at all.
"""

from __future__ import annotations

import httpx
import pytest

from src.circuit_breaker import BreakerState, CircuitOpenError
from src.data_sources.breakers import (
    COMPANIES_HOUSE,
    TAVILY,
    breaker_snapshots,
    get_breaker,
    guarded,
    is_companies_house_transient,
    is_tavily_transient,
)
from src.data_sources.companies_house import (
    CompaniesHouseAuthError,
    CompaniesHouseClient,
    CompaniesHouseError,
    CompaniesHouseUnavailable,
    CompanyNotFound,
    CompanyProfile,
)
from src.data_sources.failures import FailureKind
from src.data_sources.registry_gather import RegistrySource, gather_companies_house
from src.data_sources.tavily import (
    TavilyClient,
    TavilyError,
    TavilyQuotaExhausted,
    TavilyUnavailable,
)
from src.data_sources.web_gather import WebSection, gather_web

PROFILE = CompanyProfile.model_validate(
    {"company_number": "09446231", "company_name": "MONZO BANK LIMITED", "company_status": "active"}
)


def _ch_client(status: int) -> CompaniesHouseClient:
    return CompaniesHouseClient(
        client=httpx.AsyncClient(
            base_url="https://ch.test",
            transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})),
        )
    )


def _tavily_client(status: int) -> TavilyClient:
    return TavilyClient(
        api_key="k",
        client=httpx.AsyncClient(
            base_url="https://tavily.test",
            transport=httpx.MockTransport(lambda r: httpx.Response(status, json={})),
        ),
    )


# --------------------------------------------------------------------------
# What may and may not trip a breaker
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (CompaniesHouseUnavailable("503"), True),
        (CompanyNotFound("no such company"), False),  # a reportable fact
        (CompaniesHouseAuthError("401"), False),  # our misconfiguration
        (CompaniesHouseError("400"), False),  # our malformed request
    ],
)
def test_only_the_registers_outages_count_as_transient(error: Exception, transient: bool) -> None:
    assert is_companies_house_transient(error) is transient


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (TavilyUnavailable("503"), True),
        (TavilyQuotaExhausted("out of credits"), True),  # judgement call, see module docstring
        (TavilyError("400"), False),
    ],
)
def test_tavily_transience_includes_exhaustion(error: Exception, transient: bool) -> None:
    assert is_tavily_transient(error) is transient


@pytest.mark.asyncio
async def test_a_non_transient_failure_leaves_the_breaker_closed() -> None:
    """Phase 0.6's rule, applied to a second kind of provider: if our own bug
    trips the breaker, we stop calling a healthy register and end up debugging
    the wrong system entirely."""
    breaker = get_breaker(COMPANIES_HOUSE)

    async def boom():
        raise CompanyNotFound("nope")

    for _ in range(5):
        with pytest.raises(CompanyNotFound):
            await guarded(breaker, boom, is_transient=is_companies_house_transient)

    assert breaker.state is BreakerState.CLOSED
    assert breaker.consecutive_failures == 0


@pytest.mark.asyncio
async def test_transient_failures_trip_and_then_the_call_is_skipped() -> None:
    breaker = get_breaker(COMPANIES_HOUSE)
    called = 0

    async def boom():
        nonlocal called
        called += 1
        raise CompaniesHouseUnavailable("503")

    for _ in range(3):
        with pytest.raises(CompaniesHouseUnavailable):
            await guarded(breaker, boom, is_transient=is_companies_house_transient)

    assert breaker.state is BreakerState.OPEN

    # The fourth call never reaches the register at all.
    with pytest.raises(CircuitOpenError):
        await guarded(breaker, boom, is_transient=is_companies_house_transient)
    assert called == 3


@pytest.mark.asyncio
async def test_nothing_is_scheduled_when_the_breaker_is_open() -> None:
    """`guarded` takes a factory, not an awaitable — so a skipped call is never
    even constructed. Failing fast is the entire point of an open breaker."""
    breaker = get_breaker(TAVILY)
    for _ in range(3):
        with pytest.raises(TavilyUnavailable):
            await guarded(
                breaker,
                lambda: _raise(TavilyUnavailable("503")),
                is_transient=is_tavily_transient,
            )

    built = False

    def factory():
        nonlocal built
        built = True
        return _raise(TavilyUnavailable("503"))

    with pytest.raises(CircuitOpenError):
        await guarded(breaker, factory, is_transient=is_tavily_transient)
    assert not built


async def _raise(exc: Exception):
    raise exc


@pytest.mark.asyncio
async def test_a_success_clears_accumulated_failures() -> None:
    breaker = get_breaker(COMPANIES_HOUSE)

    for _ in range(2):
        with pytest.raises(CompaniesHouseUnavailable):
            await guarded(
                breaker,
                lambda: _raise(CompaniesHouseUnavailable("503")),
                is_transient=is_companies_house_transient,
            )
    assert breaker.consecutive_failures == 2

    async def ok():
        return "fine"

    assert await guarded(breaker, ok, is_transient=is_companies_house_transient) == "fine"
    assert breaker.consecutive_failures == 0


# --------------------------------------------------------------------------
# Isolation between sources
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_source_going_down_does_not_silence_the_other() -> None:
    """A Tavily outage must not suppress the register. Otherwise a web-search
    failure would take out the strongest evidence in the system and the report
    would render thin for a reason unrelated to the register."""
    tavily = get_breaker(TAVILY)
    for _ in range(3):
        with pytest.raises(TavilyUnavailable):
            await guarded(
                tavily, lambda: _raise(TavilyUnavailable("503")), is_transient=is_tavily_transient
            )

    assert tavily.state is BreakerState.OPEN
    assert get_breaker(COMPANIES_HOUSE).state is BreakerState.CLOSED


def test_the_same_breaker_is_returned_for_a_source() -> None:
    """One breaker per provider, not per call site — state that resets per
    caller would never accumulate enough failures to trip."""
    assert get_breaker(COMPANIES_HOUSE) is get_breaker(COMPANIES_HOUSE)
    assert get_breaker(COMPANIES_HOUSE) is not get_breaker(TAVILY)


def test_health_reports_only_breakers_that_exist() -> None:
    """An empty list means nothing has been called yet — which is different from
    every source being healthy, and must not read as it."""
    assert breaker_snapshots() == []

    get_breaker(TAVILY)
    assert [s["name"] for s in breaker_snapshots()] == [TAVILY]


# --------------------------------------------------------------------------
# End to end: an open breaker becomes a stated gap, not a crash
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_open_register_breaker_produces_stated_gaps() -> None:
    """Three sub-calls, one dead register: the report says all three sources are
    missing rather than rendering as though the company simply has no filings,
    no officers and no owners."""
    async with _ch_client(503) as client:
        first = await gather_companies_house(PROFILE, client)
        assert set(first.missing) == set(RegistrySource)  # tripped it
        assert get_breaker(COMPANIES_HOUSE).state is BreakerState.OPEN

        second = await gather_companies_house(PROFILE, client)

    assert set(second.missing) == set(RegistrySource)
    assert all(f.kind is FailureKind.UNAVAILABLE for f in second.failures)
    assert all("circuit is open" in f.detail for f in second.failures)


@pytest.mark.asyncio
async def test_an_open_tavily_breaker_skips_searches_and_says_so() -> None:
    """Once credits are gone, every later report would otherwise fire three
    doomed searches and wait on three rejections to print the same gap."""
    client = _tavily_client(429)
    async with client:
        first = await gather_web(PROFILE, client)
        assert all(f.kind is FailureKind.EXHAUSTED for f in first.failures)
        assert get_breaker(TAVILY).state is BreakerState.OPEN

        second = await gather_web(PROFILE, client)

    assert second.sections == []
    assert set(second.missing) == {s.value for s in WebSection}
    assert all("circuit is open" in f.detail for f in second.failures)


@pytest.mark.asyncio
async def test_an_auth_failure_still_raises_through_the_breaker() -> None:
    """The breaker must not convert our misconfiguration into a quiet gap — a
    rejected key would otherwise print "no register data" for every company."""
    async with _ch_client(401) as client:
        with pytest.raises(CompaniesHouseAuthError):
            await gather_companies_house(PROFILE, client)

    assert get_breaker(COMPANIES_HOUSE).state is BreakerState.CLOSED
