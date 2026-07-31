"""Companies House client — the register is the spine of every report.

Phase 1 needed two operations: search by name, look up by number. Phase 2.1
added the deep pull — filing history, officers, and persons with significant
control — as three more read-only endpoints on the same client, because they
share the auth scheme, the timeout policy and, most importantly, the status
classification below. A second client would mean a second place for that
classification to be got wrong.

Two design points worth stating, because both are load-bearing later:

1. **Nothing leaves this module as Companies House JSON.** The API's shapes are
   parsed into our own models at the boundary. Phase 1.2 ranks candidates on
   status, SIC codes and incorporation date; those fields have to be typed and
   present at parse time, not indexed out of a dict and hoped for.

2. **The three failure kinds are different answers, not one error.** "No such
   company" is a legitimate finding to put in a report. "Your key is wrong" is
   our misconfiguration and must fail loudly at us. "The register is down" must
   never be rendered as thin evidence — that is the failure mode BUILD_LOG
   names as the project's worst, where a broken system is indistinguishable
   from an honest report about a poorly-documented company.

This client deliberately has no retry or backoff. A 429 surfaces as
`CompaniesHouseUnavailable` carrying its `Retry-After`; deciding what to do
about it is Phase 2.4, so Companies House and Tavily get one shared policy
instead of two that drifted apart. It also holds no module-level singleton:
an `httpx.AsyncClient` cannot be closed from a synchronous reset hook, so a
process global here would leak connections in the test suite. Callers own the
lifetime, via `async with`.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import get_settings

logger = logging.getLogger(__name__)

BASE_URL = "https://api.company-information.service.gov.uk"

# The three list endpoints differ only in which model their rows parse into, so
# the fetch-and-parse helper is written once and typed over that. Declared as a
# TypeVar rather than PEP 695 syntax because CI runs Python 3.11.
ItemT = TypeVar("ItemT", bound=BaseModel)

# Connect fast, read patiently: the register is usually quick, but a slow
# response is still a usable one, whereas a slow *connect* means it's down.
DEFAULT_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

# Companies House caps a search page at 100.
MAX_ITEMS_PER_PAGE = 100

# How much filing history one pull takes. Deliberately a single page rather
# than a pagination loop: 100 filings reaches back roughly a decade for a
# normally-active company, the signals Phase 2.5 wants (filing punctuality, SIC
# changes, officer churn) all live in the recent end of that history, and each
# extra page is another rate-limited request against a register we also need for
# resolution. When the register says there were more, `total_count` records it,
# so a truncated history is visible rather than silently complete-looking.
FILING_HISTORY_LIMIT = 100

# 8 characters: either 8 digits, or a 2-letter jurisdiction prefix + 6 digits
# (SC = Scotland, NI = Northern Ireland, OC/SO = LLPs, and others).
_COMPANY_NUMBER_RE = re.compile(r"^(?:\d{8}|[A-Z]{2}\d{6})$")

# Legal-form suffixes carry no distinguishing information — "MONZO BANK LIMITED"
# and "Monzo Bank Ltd" are the same company to a human typing either. Lives here
# with the register's other name/number normalisation rather than in a consumer,
# because two consumers now need it: `agent.resolve` strips it to compare names,
# and `data_sources.web_gather` strips it to search the press, which writes
# "Monzo" and never "MONZO BANK LIMITED".
LEGAL_SUFFIXES = ("limited", "ltd", "plc", "llp", "lp", "cic", "cio")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class CompaniesHouseError(Exception):
    """Base for anything this module raises."""


class InvalidCompanyQuery(CompaniesHouseError):
    """The input could not be a company name or number. Never reached the API."""


class CompanyNotFound(CompaniesHouseError):
    """The register has no company under that number. A fact, not a fault."""


class CompaniesHouseAuthError(CompaniesHouseError):
    """Our credentials were rejected. Ours to fix, and it must not look transient."""


class CompaniesHouseUnavailable(CompaniesHouseError):
    """The register failed us: 5xx, 429, timeout, or connection error.

    Distinct from `CompanyNotFound` because the correct downstream behaviour is
    opposite — a missing company is reportable, an unreachable register is not.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class CompanyAddress(BaseModel):
    model_config = ConfigDict(extra="ignore")

    premises: str | None = None
    address_line_1: str | None = None
    address_line_2: str | None = None
    locality: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country: str | None = None

    def one_line(self) -> str:
        """Flattened form, for showing an underwriter which candidate is which."""
        parts = [
            self.premises,
            self.address_line_1,
            self.address_line_2,
            self.locality,
            self.region,
            self.postal_code,
            self.country,
        ]
        return ", ".join(p.strip() for p in parts if p and p.strip())


class PreviousName(BaseModel):
    """A former registered name.

    Directly load-bearing for Phase 1.3: a company that rebranded still trades
    under a legal name nobody types into a search box.
    """

    model_config = ConfigDict(extra="ignore")

    name: str
    effective_from: date | None = None
    ceased_on: date | None = None


class CompanySearchResult(BaseModel):
    """One candidate from a name search.

    `sic_codes` is optional and usually absent: the search endpoint returns a
    thinner record than the company profile does. Phase 1.2 therefore cannot
    rank on SIC relevance from search results alone — it has to fetch profiles
    for the shortlist first. Recorded here so that isn't rediscovered later.

    `snippet` and `matches` are how the register signals that it matched
    something other than the current title — in practice, a former name.
    Confirmed live: searching "Mondo" returns KNOX MORTGAGES LIMITED with
    `snippet: "MONDO MORTGAGES LIMITED"`. Parsed because it makes former-name
    candidates *discoverable* before hydration, but it is a search-relevance
    artifact with no documented stability guarantee, so nothing downstream may
    treat it as the fact itself — a former-name match is only confirmed
    against `previous_company_names` on the hydrated profile.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    number: str = Field(alias="company_number")
    name: str = Field(alias="title")
    status: str | None = Field(default=None, alias="company_status")
    company_type: str | None = Field(default=None, alias="type")
    incorporated_on: date | None = Field(default=None, alias="date_of_creation")
    address_snippet: str | None = None
    sic_codes: list[str] = Field(default_factory=list)
    snippet: str | None = None
    matches: dict[str, list[int]] | None = None


class CompanyProfile(BaseModel):
    """The full register record for one company."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    number: str = Field(alias="company_number")
    name: str = Field(alias="company_name")
    status: str | None = Field(default=None, alias="company_status")
    company_type: str | None = Field(default=None, alias="type")
    incorporated_on: date | None = Field(default=None, alias="date_of_creation")
    ceased_on: date | None = Field(default=None, alias="date_of_cessation")
    jurisdiction: str | None = None
    sic_codes: list[str] = Field(default_factory=list)
    registered_office_address: CompanyAddress | None = None
    has_insolvency_history: bool = False
    previous_names: list[PreviousName] = Field(default_factory=list, alias="previous_company_names")

    @property
    def is_active(self) -> bool:
        return self.status == "active"


class FilingRecord(BaseModel):
    """One entry in the filing history.

    `filed_on` rather than `date`: the register's field is literally named
    `date`, which would shadow `datetime.date` inside this module and make the
    annotation on every other model in it mean something different.

    Both `category` (the register's own taxonomy — `accounts`, `confirmation-
    statement`, `officers`, `mortgage`) and the free-text `description` are
    kept. The category is what Phase 2.5's punctuality and churn signals can be
    computed from mechanically; the description is what an extraction prompt can
    actually read. Dropping either would mean re-fetching to get it back.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    filed_on: date | None = Field(default=None, alias="date")
    category: str | None = None
    filing_type: str | None = Field(default=None, alias="type")
    description: str | None = None
    action_date: date | None = None
    paper_filed: bool = False


class Officer(BaseModel):
    """One appointment, current or resigned.

    Resigned officers are kept, not filtered out: turnover is itself the signal
    Phase 2.5 is after, and a list of only current officers cannot show it.
    `resigned_on` is what distinguishes them, so the filter stays available to
    the consumer rather than being applied here.

    `date_of_birth` is deliberately not parsed. The register exposes only month
    and year, it has no bearing on any of the three report sections, and there
    is no reason for personal data to enter this pipeline — an underwriting
    report about a company does not need an officer's age to be defensible.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    role: str | None = Field(default=None, alias="officer_role")
    appointed_on: date | None = None
    resigned_on: date | None = None
    nationality: str | None = None
    occupation: str | None = None
    country_of_residence: str | None = None

    @property
    def is_active(self) -> bool:
        return self.resigned_on is None


class PersonWithSignificantControl(BaseModel):
    """A beneficial owner: who actually controls this company.

    `name` is optional because it genuinely can be absent. The register omits it
    for a "super-secure" PSC — someone whose details are protected because
    disclosure would put them at risk — and for statement-type entries such as
    "the company knows of no person with significant control". Both are
    meaningful findings for an underwriter, so they are parsed and surfaced with
    their `kind` rather than dropped as malformed rows.
    """

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str | None = None
    kind: str | None = None
    natures_of_control: list[str] = Field(default_factory=list)
    notified_on: date | None = None
    ceased_on: date | None = None

    @property
    def is_active(self) -> bool:
        return self.ceased_on is None


# Statuses that mean the company is no longer trading in its own right. Kept as
# a set of strings rather than an enum on purpose: Companies House can add a
# status at any time, and an unknown value must degrade to "not recognised as
# dissolved" rather than crash resolution with a validation error.
DISSOLVED_STATUSES = frozenset(
    {"dissolved", "converted-closed", "closed", "removed", "liquidation", "receivership"}
)


def is_dissolved(status: str | None) -> bool:
    return status in DISSOLVED_STATUSES


# --------------------------------------------------------------------------
# Input normalisation
# --------------------------------------------------------------------------


def normalise_company_number(raw: str) -> str:
    """Turn what a human types into what the register expects.

    Company numbers are 8 characters, but people type `9446231` for Monzo, not
    `09446231` — the leading zero is invisible in most contexts a number gets
    copied from. Without this, lookup-by-number 404s on the most natural input
    a user could give, which would read as "no such company".
    """
    cleaned = re.sub(r"[\s-]", "", raw or "").upper()
    if not cleaned:
        raise InvalidCompanyQuery("Company number is empty")

    if cleaned.isdigit():
        cleaned = cleaned.zfill(8)
    else:
        match = re.match(r"^([A-Z]{2})(\d+)$", cleaned)
        if match:
            cleaned = match.group(1) + match.group(2).zfill(6)

    if not _COMPANY_NUMBER_RE.match(cleaned):
        raise InvalidCompanyQuery(f"Not a valid company number: {raw!r}")
    return cleaned


def looks_like_company_number(raw: str) -> bool:
    """True if this input should be treated as a number rather than a name."""
    try:
        normalise_company_number(raw)
    except InvalidCompanyQuery:
        return False
    return True


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class CompaniesHouseClient:
    """Async client for the two endpoints Phase 1 needs.

    Auth is HTTP Basic with the API key as username and an empty password —
    confirmed against the live register while building the Phase 0.7 mock.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            auth=(api_key or get_settings().companies_house_api_key, ""),
            timeout=timeout,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> CompaniesHouseClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the transport, but only if we opened it."""
        if self._owns_client:
            await self._client.aclose()

    async def _get(self, path: str, params: dict[str, object] | None = None) -> dict:
        try:
            response = await self._client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise CompaniesHouseUnavailable(f"Companies House timed out on {path}") from exc
        except httpx.TransportError as exc:
            raise CompaniesHouseUnavailable(
                f"Could not reach Companies House on {path}: {exc}"
            ) from exc

        self._raise_for_status(response, path)

        try:
            return response.json()
        except ValueError as exc:
            # A 200 that isn't JSON is the register misbehaving, not a company
            # that doesn't exist — so it must not be reportable as a finding.
            raise CompaniesHouseUnavailable(f"Companies House returned non-JSON on {path}") from exc

    @staticmethod
    def _raise_for_status(response: httpx.Response, path: str) -> None:
        status = response.status_code
        if status < 400:
            return

        if status in (401, 403):
            raise CompaniesHouseAuthError(
                f"Companies House rejected our credentials ({status}) on {path}"
            )
        if status == 404:
            raise CompanyNotFound(f"Companies House has no record at {path}")
        if status == 429:
            retry_after = response.headers.get("Retry-After")
            logger.warning("companies house rate limited on %s (retry-after=%s)", path, retry_after)
            raise CompaniesHouseUnavailable(
                f"Companies House rate limit hit on {path}",
                retry_after=float(retry_after) if retry_after and retry_after.isdigit() else None,
            )
        if status >= 500:
            raise CompaniesHouseUnavailable(f"Companies House returned {status} on {path}")

        raise CompaniesHouseError(f"Companies House returned {status} on {path}")

    async def search_companies(self, name: str, *, limit: int = 20) -> list[CompanySearchResult]:
        """Search the register by name. Returns [] when nothing matches.

        An empty list is a real answer — Phase 1.3 turns it into a "no match"
        outcome, including the rebrand/trading-name case — so it is not an error.
        """
        query = (name or "").strip()
        if not query:
            raise InvalidCompanyQuery("Search query is empty")

        payload = await self._get(
            "/search/companies",
            params={"q": query, "items_per_page": max(1, min(limit, MAX_ITEMS_PER_PAGE))},
        )

        results: list[CompanySearchResult] = []
        for item in payload.get("items") or []:
            try:
                results.append(CompanySearchResult.model_validate(item))
            except ValidationError:
                # One malformed row must not sink the whole search: dropping a
                # candidate degrades the result, raising would deny the user any
                # result at all. Logged so it can't degrade silently.
                logger.warning("skipping unparseable search result: %r", item)
        return results

    async def _get_items(
        self,
        path: str,
        model: type[ItemT],
        *,
        limit: int,
        what: str,
    ) -> tuple[list[ItemT], int | None]:
        """Fetch one page of a list endpoint, parsed, with the register's total.

        The total is returned alongside the items rather than being discarded,
        because it is the only way to tell a company with 40 filings from a
        company with 400 whose history we truncated at 100. Without it, a capped
        pull looks identical to a complete one, and Phase 5 would report on a
        partial history as though it were the whole record.

        Unparseable rows are skipped and logged, matching `search_companies`:
        one odd row degrading a list is better than it denying the caller every
        other row in that list.
        """
        payload = await self._get(
            path, params={"items_per_page": max(1, min(limit, MAX_ITEMS_PER_PAGE))}
        )

        items: list[ItemT] = []
        for item in payload.get("items") or []:
            try:
                items.append(model.model_validate(item))
            except ValidationError:
                logger.warning("skipping unparseable %s row on %s: %r", what, path, item)

        total = payload.get("total_count")
        return items, total if isinstance(total, int) else None

    async def get_filing_history(
        self, number: str, *, limit: int = FILING_HISTORY_LIMIT
    ) -> tuple[list[FilingRecord], int | None]:
        """Recent filings, most recent first, with the register's total count."""
        normalised = normalise_company_number(number)
        return await self._get_items(
            f"/company/{normalised}/filing-history",
            FilingRecord,
            limit=limit,
            what="filing",
        )

    async def get_officers(
        self, number: str, *, limit: int = MAX_ITEMS_PER_PAGE
    ) -> tuple[list[Officer], int | None]:
        """Officer appointments, including resigned ones — see `Officer`."""
        normalised = normalise_company_number(number)
        return await self._get_items(
            f"/company/{normalised}/officers",
            Officer,
            limit=limit,
            what="officer",
        )

    async def get_psc(
        self, number: str, *, limit: int = MAX_ITEMS_PER_PAGE
    ) -> tuple[list[PersonWithSignificantControl], int | None]:
        """Persons with significant control.

        Raises `CompanyNotFound` when the register holds no PSC record for the
        company — which, unlike everywhere else in this module, is the ordinary
        case rather than an anomaly, and the caller is expected to read it as
        "none recorded". Left as an exception rather than smoothed to an empty
        list here so the client keeps one uniform status classification and the
        interpretation stays with the layer that has the context to make it.
        """
        normalised = normalise_company_number(number)
        return await self._get_items(
            f"/company/{normalised}/persons-with-significant-control",
            PersonWithSignificantControl,
            limit=limit,
            what="psc",
        )

    async def get_company_profile(self, number: str) -> CompanyProfile:
        """Look up one company by number. Raises `CompanyNotFound` if absent."""
        normalised = normalise_company_number(number)
        payload = await self._get(f"/company/{normalised}")
        try:
            return CompanyProfile.model_validate(payload)
        except ValidationError as exc:
            # Unlike a search row, there is nothing to degrade to here — and
            # silently returning a half-parsed profile would let Phase 2 build
            # claims on fields that were never there.
            raise CompaniesHouseError(f"Unexpected company profile shape for {normalised}") from exc
