"""Tavily search client — the only source of evidence outside the register.

Structured deliberately as a near-mirror of `companies_house.py`: same async
httpx shape, same caller-owns-the-lifetime rule, same parse-at-the-boundary
discipline, and the same error split. That symmetry is not tidiness for its own
sake — Phase 2.4 gives each source its own circuit breaker instance driven by
exactly this classification, and two sources that classify failures differently
would need two breaker policies.

One failure kind exists here that the register has no equivalent of:
`TavilyQuotaExhausted`. Running out of free-tier credits is *our* budget
problem, not the provider's outage, and the distinction matters more here than
anywhere else in the codebase. Exhaustion produces no error the user sees — every
subsequent report simply renders with no web evidence, LOW confidence and almost
no citations, which is indistinguishable from an honest report about a
thinly-documented company. BUILD_LOG has named that the project's worst failure
mode since Phase 0. So exhaustion is a distinct exception, it is recorded on the
quota tracker that `/health` exposes, and it must reach the report as a stated
gap rather than an absence of evidence.

**Credit accounting.** Tavily bills per search, by depth: basic costs 1 credit,
advanced costs 2. The free tier is 1,000 credits/month. Those numbers drive the
global report budget in `config.py` and are recorded on the tracker as searches
run, because the free tier exposes no dependable credits-remaining endpoint —
the same constraint Phase 0 hit and the same answer: observe what we actually
spent rather than poll a balance that isn't offered.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from enum import StrEnum

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.config import get_settings
from src.data_sources.search_quota import tracker

logger = logging.getLogger(__name__)

BASE_URL = "https://api.tavily.com"

# Web search is slower than a register lookup and worth waiting for, but a slow
# connect still means the provider is unreachable.
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)

# Results per query. Bounded by Phase 3.2's tool-call budget rather than by what
# Tavily would return: more results is more extraction cost per section, and the
# tail of a web search is where the general_web noise lives.
MAX_RESULTS = 5


class SearchDepth(StrEnum):
    """Tavily's two retrieval modes, and what each costs us.

    Kept as an explicit enum with the credit cost attached because the choice is
    a budget decision, not a tuning knob — see `web_gather` for which query gets
    which depth and why.
    """

    BASIC = "basic"
    ADVANCED = "advanced"

    @property
    def credits(self) -> int:
        return 2 if self is SearchDepth.ADVANCED else 1


# --------------------------------------------------------------------------
# Errors — mirroring companies_house.py's split, plus quota
# --------------------------------------------------------------------------


class TavilyError(Exception):
    """Base for anything this module raises."""


class TavilyAuthError(TavilyError):
    """Our key was rejected. Ours to fix, and it must not look transient."""


class TavilyQuotaExhausted(TavilyError):
    """We are out of credits. Our budget, not their outage.

    Separate from `TavilyUnavailable` because the correct responses differ: an
    outage is worth a breaker cooldown and a later retry, whereas exhaustion
    will not resolve on its own and retrying only burns time. Both must be
    stated in the report; neither may render as thin evidence.
    """


class TavilyUnavailable(TavilyError):
    """Tavily failed us: 5xx, timeout, or connection error."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------


class SearchResult(BaseModel):
    """One search hit, parsed at the boundary.

    `content` is Tavily's extracted snippet, not the full page. Raw page content
    is deliberately not requested: it multiplies response size and extraction
    cost for text Phase 4 mostly discards, and BUILD_LOG records that several of
    the most relevant domains (Trustpilot, Google Play, FinTech Magazine) 403
    direct fetches anyway — so full-content retrieval is exactly the thing most
    likely to fail unevenly across sources and leave one section thinner than
    another for reasons that have nothing to do with the company.

    `retrieved_at` is stamped here, at the moment of retrieval, rather than when
    the report is rendered. Phase 2.3 requires every document to carry when it
    was fetched, and a timestamp applied later would describe the report run,
    not the evidence.
    """

    model_config = ConfigDict(extra="ignore")

    url: str
    title: str | None = None
    content: str = ""
    score: float | None = None
    published_date: str | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class SearchResponse(BaseModel):
    """The results of one query, with what it cost."""

    query: str
    depth: SearchDepth
    results: list[SearchResult] = Field(default_factory=list)
    credits_spent: int = 0


class TavilyClient:
    """Async client for Tavily's search endpoint.

    Auth is a bearer token. No retry and no backoff, matching
    `CompaniesHouseClient`: a `Retry-After` is surfaced on the exception so
    Phase 2.4 can set one policy for every source instead of two that drift.
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
        self._api_key = api_key or get_settings().tavily_api_key
        self._client = client or httpx.AsyncClient(base_url=self._base_url, timeout=timeout)

    async def __aenter__(self) -> TavilyClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        query: str,
        *,
        depth: SearchDepth = SearchDepth.BASIC,
        max_results: int = MAX_RESULTS,
    ) -> SearchResponse:
        """Run one search. Every outcome is recorded on the quota tracker."""
        text = (query or "").strip()
        if not text:
            raise TavilyError("Search query is empty")

        payload = {
            "query": text,
            "search_depth": depth.value,
            "max_results": max_results,
            "include_answer": False,
            "include_raw_content": False,
        }

        try:
            # Auth goes on the request, not on the transport. A caller that
            # injects its own `httpx.AsyncClient` — which every test does, and
            # which the gather layer may — would otherwise send no key at all,
            # and Tavily answers that with the same 401 a revoked key gets. That
            # is the exact trap Phase 1 hit with Companies House's test-vs-live
            # keys, and it cost real debugging time; auth that travels with the
            # request cannot be lost by swapping the transport underneath it.
            response = await self._client.post(
                "/search",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        except httpx.TimeoutException as exc:
            tracker.record_error("timeout")
            raise TavilyUnavailable(f"Tavily timed out on {text!r}") from exc
        except httpx.TransportError as exc:
            tracker.record_error(str(exc))
            raise TavilyUnavailable(f"Could not reach Tavily: {exc}") from exc

        self._raise_for_status(response, text)

        try:
            body = response.json()
        except ValueError as exc:
            tracker.record_error("non-JSON response")
            raise TavilyUnavailable("Tavily returned a non-JSON response") from exc

        results: list[SearchResult] = []
        for item in body.get("results") or []:
            try:
                results.append(SearchResult.model_validate(item))
            except ValidationError:
                # Same rule as the register's search: one odd row degrades the
                # result list rather than denying the caller the rest of it.
                logger.warning("skipping unparseable Tavily result: %r", item)

        tracker.record_success(credits=depth.credits)
        logger.info(
            "tavily %r (%s): %d result(s), %d credit(s)",
            text,
            depth.value,
            len(results),
            depth.credits,
        )
        return SearchResponse(query=text, depth=depth, results=results, credits_spent=depth.credits)

    @staticmethod
    def _raise_for_status(response: httpx.Response, query: str) -> None:
        status = response.status_code
        if status < 400:
            return

        if status in (401, 403):
            # Not recorded as a quota problem: a bad key is our misconfiguration
            # and must fail loudly rather than being absorbed into "we're out of
            # credits", which reads as an expected end-of-month condition.
            raise TavilyAuthError(f"Tavily rejected our key ({status})")

        if status in (402, 429):
            # Both are treated as exhaustion. 402 is unambiguous; 429 is not —
            # it can mean short-term rate limiting rather than an exhausted
            # plan, and this has not been observed against a genuinely
            # exhausted free-tier account, only reasoned from the documented
            # status codes. Treating an ambiguous 429 as exhaustion is the safe
            # direction: it over-reports a stated gap in the output, where the
            # opposite error would silently under-report evidence loss.
            retry_after = response.headers.get("Retry-After")
            detail = f"Tavily returned {status} on {query!r}"
            tracker.record_exhausted(detail)
            logger.warning("tavily quota/rate limit: %s (retry-after=%s)", detail, retry_after)
            raise TavilyQuotaExhausted(detail)

        if status >= 500:
            tracker.record_error(f"{status}")
            raise TavilyUnavailable(f"Tavily returned {status}")

        # A 4xx from our own malformed request. Classifying it as "unavailable"
        # would be the mistake Phase 0.6's breaker exists to avoid: our bug
        # wearing the costume of a provider outage.
        tracker.record_error(f"{status}")
        raise TavilyError(f"Tavily returned {status} on {query!r}")
