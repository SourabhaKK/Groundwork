"""Tests for the three-query web gather (Phase 2.2).

Two properties carry the weight here. First, the same partial tolerance the
register gather has: one section's search failing must not cost the other two,
and must leave a stated gap rather than an empty list that reads as "nothing was
written about this company". Second, the credit budget — the per-report cost is
what `config.global_report_budget` is derived from, so a depth changed casually
in one dict silently invalidates the cap, and a test is the only thing that
notices.
"""

from __future__ import annotations

import httpx
import pytest

from src.config import get_settings
from src.data_sources.companies_house import CompanyProfile
from src.data_sources.failures import FailureKind
from src.data_sources.tavily import SearchDepth, TavilyAuthError, TavilyClient
from src.data_sources.web_gather import (
    BASELINE_CREDITS_PER_REPORT,
    SECTION_DEPTH,
    WebSection,
    build_queries,
    gather_web,
    trading_name,
)

PROFILE = CompanyProfile.model_validate(
    {
        "company_number": "09446231",
        "company_name": "MONZO BANK LIMITED",
        "company_status": "active",
    }
)


def _client_returning(per_query: dict[str, tuple[int, object]] | None = None, default=(200, None)):
    """A Tavily client that answers differently depending on the query text.

    Routing on the query is what lets a test fail exactly one section: the three
    searches are otherwise identical POSTs to the same path.
    """
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        seen.append(body)
        for marker, (status, payload) in (per_query or {}).items():
            if marker in body:
                return httpx.Response(status, json=payload if payload is not None else {})
        status, payload = default
        return httpx.Response(
            status,
            json=payload if payload is not None else {"results": [{"url": "https://x.test"}]},
        )

    client = TavilyClient(
        api_key="k",
        client=httpx.AsyncClient(
            base_url="https://tavily.test", transport=httpx.MockTransport(handler)
        ),
    )
    return client, seen


# --------------------------------------------------------------------------
# Query construction
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("registered", "expected"),
    [
        ("MONZO BANK LIMITED", "Monzo Bank"),
        ("Starling Bank Ltd", "Starling Bank"),
        ("ACME HOLDINGS PLC", "Acme Holdings"),
        ("Something LLP", "Something"),
    ],
)
def test_trading_name_strips_legal_form(registered: str, expected: str) -> None:
    """The register holds "MONZO BANK LIMITED"; the press writes "Monzo". A
    query built from the registered name alone searches for a string no
    journalist ever types — Phase 1's Mondo finding in reverse."""
    assert trading_name(registered) == expected


def test_trading_name_never_invents_a_brand() -> None:
    """It strips legal form and nothing else. Phase 1 established that a trading
    name never registered is unreachable from the register; guessing one here
    would fabricate a fact."""
    assert trading_name("FOCUS FS LIMITED") == "Focus Fs"


def test_stripping_never_returns_an_empty_query() -> None:
    """An empty trading name would send an empty query to Tavily, which raises —
    turning a strange company name into three failed searches and a report with
    no web evidence at all. The fallback keeps something searchable."""
    assert trading_name("!!!") == "!!!"
    assert trading_name("LIMITED") == "Limited"  # a lone suffix is left as the name


def test_queries_carry_the_right_names() -> None:
    queries = build_queries(PROFILE)

    # Business model anchors identity with both names...
    assert "Monzo Bank" in queries[WebSection.BUSINESS_MODEL]
    assert "MONZO BANK LIMITED" in queries[WebSection.BUSINESS_MODEL]
    # ...while the other two use the brand alone, since pinning the legal name
    # into them returns filings and directory listings, not commentary.
    assert "MONZO BANK LIMITED" not in queries[WebSection.COMPETITIVE_LANDSCAPE]
    assert "MONZO BANK LIMITED" not in queries[WebSection.REPUTATION]


def test_there_is_exactly_one_query_per_report_section() -> None:
    assert set(build_queries(PROFILE)) == set(WebSection)


# --------------------------------------------------------------------------
# The credit budget the report cap is derived from
# --------------------------------------------------------------------------


def test_per_report_credit_cost_is_what_the_budget_assumes() -> None:
    """5 credits: one basic search plus two advanced. If a depth changes, this
    fails — which is the point. The global report cap is worked back from this
    number, and Phase 0's cap was wrong precisely because the two drifted."""
    assert SECTION_DEPTH[WebSection.BUSINESS_MODEL] is SearchDepth.BASIC
    assert SECTION_DEPTH[WebSection.COMPETITIVE_LANDSCAPE] is SearchDepth.ADVANCED
    assert SECTION_DEPTH[WebSection.REPUTATION] is SearchDepth.ADVANCED
    assert BASELINE_CREDITS_PER_REPORT == 5


def test_the_report_cap_still_fits_inside_the_free_tier() -> None:
    """The arithmetic that justifies the cap, pinned so it cannot silently stop
    closing: worst case is baseline + one Phase 3.5 re-search, and 200 credits
    are reserved for development and Phase 7.1 runs that bypass the gate."""
    monthly_free_tier = 1_000
    dev_and_manual_reserve = 200
    worst_case_per_report = BASELINE_CREDITS_PER_REPORT + 1  # + one basic re-search

    budget = get_settings().global_report_budget
    assert budget * worst_case_per_report <= monthly_free_tier - dev_and_manual_reserve


@pytest.mark.asyncio
async def test_gather_spends_and_reports_the_expected_credits() -> None:
    client, _ = _client_returning()
    async with client:
        gather = await gather_web(PROFILE, client)

    assert gather.credits_spent == BASELINE_CREDITS_PER_REPORT


# --------------------------------------------------------------------------
# Gathering, and partial tolerance
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_three_sections_are_searched_concurrently() -> None:
    client, seen = _client_returning()
    async with client:
        gather = await gather_web(PROFILE, client)

    assert len(seen) == 3
    assert gather.is_complete
    assert {s.section for s in gather.sections} == set(WebSection)


@pytest.mark.asyncio
async def test_results_stay_grouped_by_section() -> None:
    """Phase 4.2 frames extraction per source type and Phase 5.4 scores per
    section — a flattened list would have to be re-partitioned by both, and the
    partition is only recoverable if it's kept."""
    client, _ = _client_returning(
        {
            "competitors": (200, {"results": [{"url": "https://competitor-analysis.test"}]}),
        }
    )
    async with client:
        gather = await gather_web(PROFILE, client)

    urls = [r.url for r in gather.results_for(WebSection.COMPETITIVE_LANDSCAPE)]
    assert urls == ["https://competitor-analysis.test"]


@pytest.mark.asyncio
async def test_one_failing_section_leaves_the_others_intact() -> None:
    client, seen = _client_returning({"reviews": (503, {})})
    async with client:
        gather = await gather_web(PROFILE, client)

    assert len(seen) == 3  # nothing was cancelled mid-flight
    assert len(gather.sections) == 2
    assert gather.missing == [WebSection.REPUTATION]
    assert gather.failures[0].kind is FailureKind.UNAVAILABLE
    assert not gather.is_complete


@pytest.mark.asyncio
async def test_exhausted_credits_are_a_stated_gap_not_an_empty_section() -> None:
    """The failure this whole layer exists to prevent: with no credits, every
    section would otherwise render empty, LOW-confidence and uncited — exactly
    what an honest report about a thinly-documented company looks like."""
    client, _ = _client_returning(default=(429, {}))
    async with client:
        gather = await gather_web(PROFILE, client)

    assert gather.sections == []
    assert set(gather.missing) == {s.value for s in WebSection}
    assert all(f.kind is FailureKind.EXHAUSTED for f in gather.failures)


@pytest.mark.asyncio
async def test_exhaustion_is_distinguishable_from_an_outage() -> None:
    """They read the same in a report but Phase 2.4 must respond differently:
    an outage is worth a cooldown and a retry, exhaustion never resolves on its
    own and retrying only burns the run's time budget."""
    client, _ = _client_returning({"reviews": (402, {}), "competitors": (500, {})})
    async with client:
        gather = await gather_web(PROFILE, client)

    kinds = {f.source: f.kind for f in gather.failures}
    assert kinds[WebSection.REPUTATION] is FailureKind.EXHAUSTED
    assert kinds[WebSection.COMPETITIVE_LANDSCAPE] is FailureKind.UNAVAILABLE


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_a_rejected_key_raises_rather_than_becoming_three_gaps(status: int) -> None:
    """Our misconfiguration would otherwise be recorded as three separate
    "no coverage found" gaps in a report that should not have been produced."""
    client, _ = _client_returning(default=(status, {}))
    async with client:
        with pytest.raises(TavilyAuthError):
            await gather_web(PROFILE, client)


@pytest.mark.asyncio
async def test_a_section_with_no_results_is_complete_not_missing() -> None:
    """A search that ran and found nothing is a finding about the company. A
    search that failed is a fact about us. Conflating them is the whole bug."""
    client, _ = _client_returning({"reviews": (200, {"results": []})})
    async with client:
        gather = await gather_web(PROFILE, client)

    assert gather.is_complete
    assert gather.results_for(WebSection.REPUTATION) == []
    assert len(gather.sections) == 3
