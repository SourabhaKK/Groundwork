"""Tests for the Companies House deep pull (Phase 2.1).

The property under test throughout is the one that distinguishes this gather
from Phase 1's hydration: a failing source degrades to a *stated* gap rather
than either failing the whole pull or vanishing. Both failure modes it guards
against are silent by nature — a report about a company whose officers list
never loaded looks exactly like a report about a company with no officers — so
almost every test here asserts on `failures` as well as on the data.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from src.data_sources.companies_house import (
    CompaniesHouseAuthError,
    CompaniesHouseClient,
    CompanyProfile,
)
from src.data_sources.registry_gather import (
    FailureKind,
    RegistrySource,
    gather_companies_house,
)

PROFILE = CompanyProfile.model_validate(
    {
        "company_number": "09446231",
        "company_name": "MONZO BANK LIMITED",
        "company_status": "active",
        "date_of_creation": "2015-02-18",
        "sic_codes": ["64191", "64999"],
    }
)

FILINGS = {
    "total_count": 42,
    "items": [
        {
            "date": "2025-12-19",
            "type": "AA",
            "category": "accounts",
            "description": "Group of companies' accounts made up to 2025-02-28",
        },
        {
            "date": "2025-03-04",
            "type": "CS01",
            "category": "confirmation-statement",
            "description": "Confirmation statement made on 2025-02-18",
        },
    ],
}

OFFICERS = {
    "total_count": 2,
    "items": [
        {
            "name": "ANIL, Tarun",
            "officer_role": "director",
            "appointed_on": "2020-06-01",
            "nationality": "British",
            "occupation": "Banker",
            "country_of_residence": "England",
        },
        {
            "name": "FORMER, Someone",
            "officer_role": "director",
            "appointed_on": "2016-01-01",
            "resigned_on": "2019-09-30",
        },
    ],
}

PSC = {
    "total_count": 1,
    "items": [
        {
            "name": "Monzo Bank Holding Group Limited",
            "kind": "corporate-entity-person-with-significant-control",
            "natures_of_control": ["ownership-of-shares-75-to-100-percent"],
            "notified_on": "2018-05-01",
        }
    ],
}


def _routed_client(
    routes: dict[str, tuple[int, object]],
    seen: list[str] | None = None,
) -> CompaniesHouseClient:
    """A client whose three deep-pull endpoints answer independently.

    Routing per-endpoint rather than returning one canned response is what lets
    a test fail exactly one source and assert the other two survived it — the
    isolation property is not observable with a single global handler.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if seen is not None:
            seen.append(path)
        for suffix, (status, payload) in routes.items():
            if path.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return httpx.Response(status, json=payload)
        raise AssertionError(f"unexpected request to {path}")

    return CompaniesHouseClient(
        client=httpx.AsyncClient(base_url="https://ch.test", transport=httpx.MockTransport(handler))
    )


def _all_ok() -> dict[str, tuple[int, object]]:
    return {
        "/filing-history": (200, FILINGS),
        "/officers": (200, OFFICERS),
        "/persons-with-significant-control": (200, PSC),
    }


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gathers_all_three_sources_concurrently() -> None:
    seen: list[str] = []
    async with _routed_client(_all_ok(), seen) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.is_complete
    assert gather.missing == []
    assert [f.filed_on for f in gather.filings] == [date(2025, 12, 19), date(2025, 3, 4)]
    assert gather.filings[0].category == "accounts"
    assert gather.filings[0].filing_type == "AA"
    assert [o.name for o in gather.officers] == ["ANIL, Tarun", "FORMER, Someone"]
    assert gather.psc[0].natures_of_control == ["ownership-of-shares-75-to-100-percent"]
    assert len(seen) == 3


@pytest.mark.asyncio
async def test_the_profile_is_not_refetched() -> None:
    """Phase 1 already paid for the profile, and re-fetching risks the bundle
    describing a different company from the one the user confirmed."""
    seen: list[str] = []
    async with _routed_client(_all_ok(), seen) as client:
        await gather_companies_house(PROFILE, client)

    assert "/company/09446231" not in seen  # the bare profile path, not the sub-resources


@pytest.mark.asyncio
async def test_resigned_officers_are_kept() -> None:
    """Turnover is the signal; a list of only current officers cannot show it."""
    async with _routed_client(_all_ok()) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert [o.is_active for o in gather.officers] == [True, False]


# --------------------------------------------------------------------------
# Partial tolerance — one source failing must not take the others with it
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_source_degrades_to_a_stated_gap() -> None:
    routes = _all_ok() | {"/officers": (503, {})}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    # The other two survived...
    assert len(gather.filings) == 2
    assert len(gather.psc) == 1
    # ...and the gap is stated rather than silent.
    assert gather.officers == []
    assert not gather.is_complete
    assert gather.missing == [RegistrySource.OFFICERS]
    assert gather.failures[0].kind is FailureKind.UNAVAILABLE


@pytest.mark.asyncio
async def test_every_source_can_fail_without_raising() -> None:
    routes = {
        "/filing-history": (503, {}),
        "/officers": (503, {}),
        "/persons-with-significant-control": (503, {}),
    }
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert set(gather.missing) == set(RegistrySource)
    assert gather.profile.number == "09446231"  # still a usable bundle, just an empty one


@pytest.mark.asyncio
async def test_a_failing_source_does_not_cancel_the_others_in_flight() -> None:
    """`return_exceptions=True` is the mechanism; this is the behaviour it buys.
    Without it the first exception cancels the sibling tasks, and the failure
    count would depend on which request happened to finish first."""
    seen: list[str] = []
    routes = _all_ok() | {"/filing-history": (500, {})}
    async with _routed_client(routes, seen) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert len(seen) == 3
    assert len(gather.failures) == 1


@pytest.mark.asyncio
async def test_connection_error_is_recorded_not_raised() -> None:
    routes = _all_ok() | {"/officers": (0, httpx.ConnectError("refused"))}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.failures[0].kind is FailureKind.UNAVAILABLE
    assert len(gather.filings) == 2


@pytest.mark.asyncio
async def test_retry_after_is_carried_onto_the_failure_for_phase_2_4() -> None:
    """2.1 does no retrying itself. The register's own number is preserved so
    2.4 can set one policy across every source instead of guessing."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/officers"):
            return httpx.Response(429, headers={"Retry-After": "30"}, json={})
        return httpx.Response(200, json={"items": [], "total_count": 0})

    client = CompaniesHouseClient(
        client=httpx.AsyncClient(base_url="https://ch.test", transport=httpx.MockTransport(handler))
    )
    async with client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.failures[0].retry_after == 30.0
    assert gather.failures[0].kind is FailureKind.UNAVAILABLE


@pytest.mark.asyncio
async def test_malformed_request_is_an_error_not_an_outage() -> None:
    """Our bug and the register's outage must stay distinguishable, because
    2.4's breaker is about to trip on one and must never trip on the other."""
    routes = _all_ok() | {"/officers": (400, {})}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.failures[0].kind is FailureKind.ERROR


# --------------------------------------------------------------------------
# The two failures that are not degraded
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_psc_record_is_a_finding_not_a_gap() -> None:
    """The register 404s this endpoint for companies with no PSC recorded. That
    is an answer — nobody is registered as controlling this company — and
    recording it as missing data would put a caveat on the majority of reports
    and dilute the ones where something genuinely failed."""
    routes = _all_ok() | {"/persons-with-significant-control": (404, {})}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.psc == []
    assert gather.is_complete
    assert gather.failures == []


@pytest.mark.asyncio
async def test_a_404_on_filings_is_still_recorded() -> None:
    """The control for the test above: the exemption is specific to PSC, not a
    blanket rule that 404s are fine. The profile provably exists, so filing
    history should have answered."""
    routes = _all_ok() | {"/filing-history": (404, {})}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.missing == [RegistrySource.FILING_HISTORY]
    assert gather.failures[0].kind is FailureKind.NOT_FOUND


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_raises_rather_than_becoming_a_gap(status: int) -> None:
    """A rejected key is our misconfiguration, not a fact about the company.
    Degraded, it would print "no officers recorded" for every company we ever
    look at — false, and self-concealing."""
    routes = _all_ok() | {"/officers": (status, {})}
    async with _routed_client(routes) as client:
        with pytest.raises(CompaniesHouseAuthError):
            await gather_companies_house(PROFILE, client)


# --------------------------------------------------------------------------
# Truncation and parsing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_truncated_filing_history_is_visible() -> None:
    """A capped pull must not look like a complete one — otherwise a report
    describes ten years of a forty-year history as the whole record."""
    routes = _all_ok() | {"/filing-history": (200, {"total_count": 400, "items": FILINGS["items"]})}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.filings_total == 400
    assert gather.filings_truncated


@pytest.mark.asyncio
async def test_complete_filing_history_is_not_flagged_as_truncated() -> None:
    routes = _all_ok() | {"/filing-history": (200, {"total_count": 2, "items": FILINGS["items"]})}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert not gather.filings_truncated


@pytest.mark.asyncio
async def test_filing_page_size_is_capped_at_the_registers_maximum() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json={"items": [], "total_count": 0})

    client = CompaniesHouseClient(
        client=httpx.AsyncClient(base_url="https://ch.test", transport=httpx.MockTransport(handler))
    )
    async with client:
        await gather_companies_house(PROFILE, client, filing_limit=500)

    filings_url = next(u for u in seen if u.path.endswith("/filing-history"))
    assert filings_url.params["items_per_page"] == "100"


@pytest.mark.asyncio
async def test_one_unparseable_row_does_not_sink_the_list() -> None:
    """Same rule as `search_companies`: degrade the list, don't deny it."""
    routes = _all_ok() | {
        "/officers": (200, {"total_count": 2, "items": [OFFICERS["items"][0], {"nonsense": True}]})
    }
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert [o.name for o in gather.officers] == ["ANIL, Tarun"]
    assert gather.is_complete  # a dropped row is not a source failure


@pytest.mark.asyncio
async def test_a_protected_psc_is_surfaced_not_dropped() -> None:
    """A super-secure PSC has no name. That is meaningful to an underwriter —
    someone controls this company and their identity is legally protected — so
    it must not be discarded as a malformed row."""
    routes = _all_ok() | {
        "/persons-with-significant-control": (
            200,
            {
                "total_count": 1,
                "items": [{"kind": "super-secure-person-with-significant-control"}],
            },
        )
    }
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert len(gather.psc) == 1
    assert gather.psc[0].name is None
    assert gather.psc[0].kind == "super-secure-person-with-significant-control"


@pytest.mark.asyncio
async def test_missing_total_count_does_not_crash_the_gather() -> None:
    """The register omits `total_count` on some responses; a bundle that fails
    to build because a count was absent would lose data it successfully had."""
    routes = _all_ok() | {"/officers": (200, {"items": OFFICERS["items"]})}
    async with _routed_client(routes) as client:
        gather = await gather_companies_house(PROFILE, client)

    assert gather.officers_total is None
    assert len(gather.officers) == 2
