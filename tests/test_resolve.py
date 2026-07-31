"""Resolution tests (Phases 1.2 and 1.3). Phase 1.4 extends this file.

Transport is mocked with `httpx.MockTransport` rather than a live call —
per AGENTS.md, no test in this suite touches a real API.
"""

from __future__ import annotations

import httpx
import pytest

from src.agent.resolve import (
    ConfirmationReason,
    ResolutionKind,
    _name_score,
    _prescore,
    resolve_company,
)
from src.data_sources.companies_house import CompaniesHouseClient, CompanySearchResult


def _profile(
    number: str,
    name: str,
    *,
    status: str = "active",
    sic: list[str],
    previous: list[str] | None = None,
) -> dict:
    return {
        "company_number": number,
        "company_name": name,
        "company_status": status,
        "type": "ltd",
        "date_of_creation": "2015-02-18",
        "sic_codes": sic,
        # Default dates are Monzo's real ones, verified against the live
        # register: FOCUS FS LIMITED ran 2015-02-18 to 2016-10-21. Fixtures for
        # obviously-fictional companies (ACME &c.) reuse them as filler.
        "previous_company_names": [
            {"name": former, "effective_from": "2015-02-18", "ceased_on": "2016-10-21"}
            for former in previous or []
        ],
    }


def _search_item(
    number: str, name: str, *, status: str = "active", snippet: str | None = None
) -> dict:
    """One search result. `snippet` is how the live register signals that it
    matched a former name rather than the current title."""
    item = {
        "company_number": number,
        "title": name,
        "company_status": status,
        "type": "ltd",
        "date_of_creation": "2015-02-18",
    }
    if snippet is not None:
        item["snippet"] = snippet
        item["matches"] = {"snippet": []}
    return item


def _client(
    items: list[dict],
    profiles: dict[str, dict],
    *,
    relaxed_items: dict[str, list[dict]] | None = None,
) -> CompaniesHouseClient:
    """A register that returns `items` for any search, unless the query appears
    in `relaxed_items` — which is how the one relaxed retry is exercised."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/search/companies":
            query = request.url.params.get("q", "")
            if relaxed_items is not None and query in relaxed_items:
                return httpx.Response(200, json={"items": relaxed_items[query]})
            return httpx.Response(200, json={"items": items})
        return httpx.Response(200, json=profiles[request.url.path.rsplit("/", 1)[-1]])

    transport = httpx.AsyncClient(
        base_url="https://ch.test", transport=httpx.MockTransport(handler)
    )
    return CompaniesHouseClient(client=transport)


# --------------------------------------------------------------------------
# Phase 1.2 — the auto-resolve gate
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dormant_shell_does_not_auto_resolve() -> None:
    """A unique, active, exactly-named match must still not auto-resolve if it
    is SIC-coded dormant.

    "Active" on the register means "not dissolved", not "trading". Without the
    shell check in the gate, a dormant registration with the right name
    silently becomes the subject of the entire report — the SIC penalty in the
    ranking never fires, because it only sorts candidates against each other
    and there is nothing here to outrank.
    """
    async with _client(
        [_search_item("33333333", "ACME TRADING LTD")],
        {"33333333": _profile("33333333", "ACME TRADING LTD", sic=["99999"])},
    ) as client:
        result = await resolve_company("Acme Trading", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [ConfirmationReason.DORMANT_SIC]
    # Still surfaced, so a human can confirm it deliberately rather than have
    # it disappear — the point is withheld automation, not a hidden candidate.
    assert result.profile is not None
    assert result.profile.number == "33333333"


@pytest.mark.asyncio
async def test_trading_company_still_auto_resolves() -> None:
    """The control: same shape, real SIC code. Proves the gate discriminates
    rather than just refusing everything."""
    async with _client(
        [_search_item("33333333", "ACME TRADING LTD")],
        {"33333333": _profile("33333333", "ACME TRADING LTD", sic=["43999"])},
    ) as client:
        result = await resolve_company("Acme Trading", client)

    assert result.kind is ResolutionKind.RESOLVED
    assert result.profile is not None
    assert result.profile.number == "33333333"
    assert result.reasons == []


@pytest.mark.asyncio
async def test_active_exact_match_wins_over_dissolved_namesake() -> None:
    """An old dissolved "X Ltd" beside the current trading "X Ltd" is not a
    real ambiguity, and confirming it every time trains people to click
    through confirmations without reading them."""
    async with _client(
        [
            _search_item("44444444", "ACME TRADING LTD", status="dissolved"),
            _search_item("33333333", "ACME TRADING LTD"),
        ],
        {
            "33333333": _profile("33333333", "ACME TRADING LTD", sic=["43999"]),
            "44444444": _profile("44444444", "ACME TRADING LTD", status="dissolved", sic=["43999"]),
        },
    ) as client:
        result = await resolve_company("Acme Trading", client)

    assert result.kind is ResolutionKind.RESOLVED
    assert result.profile is not None
    assert result.profile.number == "33333333"


# --------------------------------------------------------------------------
# Ranking precedence — pinned, not left to emerge
# --------------------------------------------------------------------------


def test_current_name_match_outranks_another_companys_former_name() -> None:
    """A match on the company's CURRENT registered name must outrank another
    company's historical one, whatever order the register returned them in.

    Taken from live data: for q="Monzo" the register put three former-name
    matches from unrelated companies above MONZO BANK LIMITED. Our own
    pre-score reverses that — but only because `_prescore` reads `title` and
    nothing else, which is a property of today's implementation rather than a
    stated rule. Pinned here so that teaching `_prescore` about `snippet`
    later cannot silently invert it.
    """
    monzo = CompanySearchResult.model_validate(_search_item("09446231", "MONZO BANK LIMITED"))
    impostor = CompanySearchResult.model_validate(
        _search_item("15296432", "BRAMAND LTD", snippet="MONZO SOMETHING LIMITED")
    )

    assert _prescore("Monzo", monzo) > _prescore("Monzo", impostor)


@pytest.mark.asyncio
async def test_correct_entity_survives_being_outranked_by_the_register() -> None:
    """The end-to-end form of the same property: the register hands back three
    former-name matches first, and resolution still lands on the real company."""
    async with _client(
        [
            _search_item("15296432", "BRAMAND LTD", snippet="MONZO A LIMITED"),
            _search_item("14728667", "CAPIWISE LTD", snippet="MONZO B LIMITED"),
            _search_item("12808379", "12808379 LTD", status="dissolved", snippet="MONZO C LTD"),
            _search_item("09446231", "MONZO BANK LIMITED"),
        ],
        {
            "09446231": _profile("09446231", "MONZO BANK LIMITED", sic=["64191"]),
            "15296432": _profile("15296432", "BRAMAND LTD", sic=["64999"]),
            "14728667": _profile("14728667", "CAPIWISE LTD", sic=["64999"]),
            "12808379": _profile("12808379", "12808379 LTD", status="dissolved", sic=["64999"]),
        },
    ) as client:
        result = await resolve_company("Monzo Bank", client)

    assert result.kind is ResolutionKind.RESOLVED
    assert result.profile is not None
    assert result.profile.number == "09446231"


# --------------------------------------------------------------------------
# Phase 1.3 — malformed input, dissolved, no match, rebrands
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["", "   ", "!!!", "x", "A" * 200])
async def test_malformed_input_is_rejected_before_any_call(query: str) -> None:
    """Invalid input is a verdict, not an exception — and it must not spend a
    rate-limited API call to reach that verdict."""

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError(f"no request should have been made, got {request.url}")

    transport = httpx.AsyncClient(
        base_url="https://ch.test", transport=httpx.MockTransport(handler)
    )
    async with CompaniesHouseClient(client=transport) as client:
        result = await resolve_company(query, client)

    assert result.kind is ResolutionKind.INVALID
    assert result.detail  # says which problem, not just "invalid"


@pytest.mark.asyncio
async def test_dissolved_company_needs_confirmation() -> None:
    """A dissolved company resolves to itself, but never silently — an
    underwriter may legitimately want it, and must be told first."""
    async with _client(
        [_search_item("44444444", "ACME TRADING LTD", status="dissolved")],
        {"44444444": _profile("44444444", "ACME TRADING LTD", status="dissolved", sic=["43999"])},
    ) as client:
        result = await resolve_company("Acme Trading", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [ConfirmationReason.DISSOLVED]
    assert result.profile is not None


@pytest.mark.asyncio
async def test_administration_is_not_reported_as_dissolved() -> None:
    """Neither a going concern nor dead. Calling it dissolved would overstate
    what the register actually says."""
    async with _client(
        [_search_item("55555555", "ACME TRADING LTD", status="administration")],
        {
            "55555555": _profile(
                "55555555", "ACME TRADING LTD", status="administration", sic=["43999"]
            )
        },
    ) as client:
        result = await resolve_company("Acme Trading", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [ConfirmationReason.NOT_ACTIVE]


@pytest.mark.asyncio
async def test_former_name_match_needs_confirmation() -> None:
    """Typing a name the company used to hold surfaces it — as a confirmation,
    since a former name is evidence of identity, not proof of it.

    The fixture is real and checked: company 09446231 was FOCUS FS LIMITED
    from 2015-02-18 until 2016-10-21, verified against the live register.
    An earlier version of this test asserted a former name of "MONDO BANK
    LIMITED", which was invented — Mondo was Monzo's trading name and never
    a registered one, so the register has no such record. A fixture stating a
    false fact about the register is the kind of thing that gets read later as
    ground truth, which this project's boundaries forbid.
    """
    async with _client(
        # The snippet mirrors what the live register returns: searching a former
        # name surfaces the company with the matched former name in `snippet`.
        [_search_item("09446231", "MONZO BANK LIMITED", snippet="FOCUS FS LIMITED")],
        {
            "09446231": _profile(
                "09446231", "MONZO BANK LIMITED", sic=["64191"], previous=["FOCUS FS LIMITED"]
            )
        },
    ) as client:
        result = await resolve_company("Focus FS", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [ConfirmationReason.PREVIOUS_NAME]
    assert result.candidates[0].matched_previous_name == "FOCUS FS LIMITED"


@pytest.mark.asyncio
async def test_former_name_candidate_is_hydrated_despite_never_ranking() -> None:
    """The gap Fix 2 closes: a company that matched only on a former name
    scores nothing on current-name similarity, so it is crowded out of the
    shortlist by five irrelevant active companies and never hydrated — which
    made former-name resolution unreachable in exactly the case needing it.

    KNOX MORTGAGES LIMITED / "MONDO MORTGAGES LIMITED" is a real pair returned
    by the live register for q="Mondo".
    """
    filler = [_search_item(f"9000000{n}", f"MONDOVI TRADING {n} LTD") for n in range(1, 6)]
    async with _client(
        [
            *filler,
            _search_item("15306053", "KNOX MORTGAGES LIMITED", snippet="MONDO MORTGAGES LIMITED"),
        ],
        {
            **{
                f"9000000{n}": _profile(f"9000000{n}", f"MONDOVI TRADING {n} LTD", sic=["43999"])
                for n in range(1, 6)
            },
            "15306053": _profile(
                "15306053",
                "KNOX MORTGAGES LIMITED",
                sic=["64921"],
                previous=["MONDO MORTGAGES LIMITED"],
            ),
        },
    ) as client:
        result = await resolve_company("Mondo Mortgages", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [ConfirmationReason.PREVIOUS_NAME]
    assert result.profile is not None
    assert result.profile.number == "15306053"


@pytest.mark.asyncio
async def test_junk_query_returns_not_found_not_a_candidate_list() -> None:
    """The register fuzzy-matches almost anything. Live, "Nonexistent Widgets
    Xyzzy" returned AJAX WIDGETS LIMITED and XYZZY LIMITED — real companies
    with no connection to the query — and we surfaced five of them as though
    they were plausible answers, after paying five profile fetches to do it."""
    async with _client(
        [
            _search_item("08211338", "AJAX WIDGETS LIMITED"),
            _search_item("08478494", "XYZZY LIMITED"),
            _search_item("15688326", "CREATIVE WIDGETS LTD"),
        ],
        {},  # nothing should be hydrated at all
    ) as client:
        result = await resolve_company("Nonexistent Widgets Xyzzy", client)

    assert result.kind is ResolutionKind.NOT_FOUND
    assert result.candidates == []


@pytest.mark.asyncio
async def test_former_name_candidate_survives_relevance_floor() -> None:
    """The one place the relevance floor and Fix 2's discovery mechanism meet.

    A former-name candidate scores zero on current-name similarity *by design*
    — the name that matched is one the company no longer has. The floor drops
    exactly that profile of candidate, so the exemption is what keeps the
    mechanism alive. If a future change applies the floor before the exemption,
    former-name resolution dies silently and still looks like a reasonable
    "not found", so this must fail loudly rather than rely on construction.

    KNOX MORTGAGES LIMITED / "MONDO MORTGAGES LIMITED" is a real pair, returned
    by the live register for q="Mondo".
    """
    knox = _search_item("15306053", "KNOX MORTGAGES LIMITED", snippet="MONDO MORTGAGES LIMITED")
    profiles = {
        "15306053": _profile(
            "15306053",
            "KNOX MORTGAGES LIMITED",
            sic=["64921"],
            previous=["MONDO MORTGAGES LIMITED"],
        )
    }

    # "Mondo Mortgages" vs "KNOX MORTGAGES LIMITED" scores match="none", so
    # the floor would discard it were the exemption not there.
    assert _name_score("Mondo Mortgages", "KNOX MORTGAGES LIMITED")[1] == "none"

    async with _client([knox], profiles) as client:
        result = await resolve_company("Mondo Mortgages", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [ConfirmationReason.PREVIOUS_NAME]
    assert result.profile is not None
    assert result.profile.number == "15306053"

    # Control: the identical candidate without the register's former-name flag
    # is correctly dropped by the floor. Proves the exemption is doing the
    # work, not that the floor simply never fires on this input.
    unflagged = _search_item("15306053", "KNOX MORTGAGES LIMITED")
    async with _client([unflagged], profiles) as client:
        control = await resolve_company("Mondo Mortgages", client)

    assert control.kind is ResolutionKind.NOT_FOUND


@pytest.mark.asyncio
async def test_snippet_alone_never_confirms_a_former_name_match() -> None:
    """`snippet` gets a candidate hydrated; only `previous_company_names`
    confirms it. A snippet the profile doesn't corroborate must produce no
    former-name match at all — the report's claim has to trace to the typed,
    guaranteed field, not to a search-relevance artifact."""
    async with _client(
        [_search_item("15306053", "KNOX MORTGAGES LIMITED", snippet="MONDO MORTGAGES LIMITED")],
        # Profile carries NO previous_company_names — the snippet is unbacked.
        {"15306053": _profile("15306053", "KNOX MORTGAGES LIMITED", sic=["64921"])},
    ) as client:
        result = await resolve_company("Mondo Mortgages", client)

    assert result.kind is ResolutionKind.AMBIGUOUS
    assert result.candidates[0].matched_previous_name is None
    assert ConfirmationReason.PREVIOUS_NAME not in result.reasons


@pytest.mark.asyncio
async def test_reasons_accumulate_rather_than_dropping_one() -> None:
    """A dissolved company found under a former name is both things at once.
    Reporting only the first checked would hand back the less alarming half at
    random — which is why `reasons` is a list."""
    async with _client(
        [
            _search_item(
                "66666666",
                "OLDCO HOLDINGS LTD",
                status="dissolved",
                snippet="ACME TRADING LIMITED",
            )
        ],
        {
            "66666666": _profile(
                "66666666",
                "OLDCO HOLDINGS LTD",
                status="dissolved",
                sic=["99999"],
                previous=["ACME TRADING LIMITED"],
            )
        },
    ) as client:
        result = await resolve_company("Acme Trading", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [
        ConfirmationReason.PREVIOUS_NAME,
        ConfirmationReason.DISSOLVED,
        ConfirmationReason.DORMANT_SIC,
    ]


@pytest.mark.asyncio
async def test_relaxed_retry_finds_company_but_asks_first() -> None:
    """ "Acme Group UK Ltd" finds nothing; "acme" does. One retry, then stop.

    The hit is never auto-accepted: it was found by altering what the user
    asked for, and "Acme Holdings Ltd" and "Acme Ltd" are routinely different
    legal entities. Dropping the token that distinguishes them and then
    resolving silently would be a guess wearing a match's clothes.
    """
    async with _client(
        [],
        {"77777777": _profile("77777777", "ACME LIMITED", sic=["43999"])},
        relaxed_items={"acme": [_search_item("77777777", "ACME LIMITED")]},
    ) as client:
        result = await resolve_company("Acme Group UK Ltd", client)

    assert result.kind is ResolutionKind.NEEDS_CONFIRMATION
    assert result.reasons == [ConfirmationReason.RELAXED_MATCH]
    assert result.profile is not None
    assert result.profile.number == "77777777"


@pytest.mark.asyncio
async def test_no_match_says_what_to_try_next() -> None:
    """Not found is an honest answer, but a bare one is unhelpful — the most
    common cause is a trading name, so say so."""
    async with _client([], {}) as client:
        result = await resolve_company("Nonexistent Widgets", client)

    assert result.kind is ResolutionKind.NOT_FOUND
    assert result.detail is not None
    assert "registered" in result.detail
