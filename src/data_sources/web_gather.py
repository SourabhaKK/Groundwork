"""Web research for the three report sections (Phase 2.2).

Three targeted searches, one per report section, run concurrently off the
resolved company profile. Same partial-tolerance contract as the register
gather: a failing search degrades to a stated gap, never to a silent absence.

**Why three queries and not one.** The report's three sections have genuinely
different evidence needs, and a single "tell me about X" search returns a
homogeneous blob that over-serves business model and starves quality signals.
Since Phase 5.4 scores confidence *per section*, that imbalance would surface as
a thin reputation section reporting honest uncertainty when the real cause was
query design. Three queries also give Phase 3.5's adaptive re-search something
to refine — it can reissue one section's query more narrowly, which is not
possible if there was only ever one.

**Why the depths differ, and what it costs.** Tavily bills per search by depth:
basic 1 credit, advanced 2, against a 1,000-credit monthly free tier.

- Business model — **basic**. The company's own site and trade-press
  descriptions rank on the first page; depth buys little.
- Competitive landscape — **advanced**. The answer is assembled from scattered
  mentions rather than sitting in one top result.
- Reputation — **advanced**. Top hits are aggregator homepages whose basic
  snippet is generic boilerplate; depth is what returns actual sentiment text.

That is 5 credits per report, 6 with a Phase 3.5 re-search, which is what
`config.global_report_budget` is worked back from.

**Why the queries are fixed templates rather than LLM-generated.** Generating
queries per company would adapt better to unusual businesses, and it is the more
agentic-looking choice. It is rejected here because it makes the gather layer
non-deterministic exactly where it must not be: every test would depend on an
LLM call, and a badly generated query would be indistinguishable from a company
with no press coverage — the project's worst failure mode, reintroduced at the
source. The adaptive behaviour is not lost, it is relocated to where TASKS.md
already puts it: Phase 3.5's re-search loop, which refines a query in response to
*measured* extraction gaps rather than guessing before any evidence exists.

**Known limitation, stated rather than hidden:** templates serve a mainstream
consumer-facing UK company well and an unusual one — a holding company, a very
new registration, a B2B firm with no consumer press — poorly. The reputation
query in particular will return little for a company with no consumer product.
Phase 3.5 mitigates this; it does not solve it, and it belongs in the design
note's deferred section.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from src.circuit_breaker import CircuitOpenError
from src.data_sources.breakers import TAVILY, get_breaker, guarded, is_tavily_transient
from src.data_sources.companies_house import LEGAL_SUFFIXES, CompanyProfile
from src.data_sources.failures import FailureKind, GatherFailure
from src.data_sources.tavily import (
    SearchDepth,
    SearchResult,
    TavilyAuthError,
    TavilyClient,
    TavilyQuotaExhausted,
    TavilyUnavailable,
)

logger = logging.getLogger(__name__)


class WebSection(StrEnum):
    """The report section a search was run for.

    Carried on every result through to extraction, so Phase 4 knows which
    section's prompt a document was gathered for and Phase 5.4 scores each
    section against the evidence actually gathered for it.
    """

    BUSINESS_MODEL = "business_model"
    COMPETITIVE_LANDSCAPE = "competitive_landscape"
    REPUTATION = "reputation"


# Depth per section, and therefore cost per section. Kept as data rather than
# buried in the call sites so the per-report credit cost can be read off in one
# place — it is the number `config.global_report_budget` is derived from.
SECTION_DEPTH: dict[WebSection, SearchDepth] = {
    WebSection.BUSINESS_MODEL: SearchDepth.BASIC,
    WebSection.COMPETITIVE_LANDSCAPE: SearchDepth.ADVANCED,
    WebSection.REPUTATION: SearchDepth.ADVANCED,
}

BASELINE_CREDITS_PER_REPORT = sum(depth.credits for depth in SECTION_DEPTH.values())


def trading_name(registered_name: str) -> str:
    """The name the press actually uses, from the name the register holds.

    Phase 1's Mondo finding, running in the other direction: the register knows
    "MONZO BANK LIMITED" while trade press writes "Monzo", so a query built from
    the registered name alone searches for a string journalists never type.
    Title-cased because search engines treat an all-caps query as an acronym.

    This only strips legal form. It does not attempt to recover a brand name
    that differs from the registered one — Phase 1 established that a trading
    name which was never registered is structurally unreachable from the
    register, and guessing one here would invent a fact.
    """
    text = re.sub(r"[^\w\s]", " ", registered_name or "")
    text = re.sub(r"\s+", " ", text).strip()

    changed = True
    while changed:
        changed = False
        for suffix in LEGAL_SUFFIXES:
            if text.lower().endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True

    if not text:
        return registered_name.strip()
    return text.title() if text.isupper() else text


def build_queries(profile: CompanyProfile) -> dict[WebSection, str]:
    """One query per section.

    The business-model query carries both names — the trading name because the
    press uses it, the registered name because it is what disambiguates a
    generic trading name from another company with the same one. The other two
    use the trading name alone: coverage of competitors and reviews is written
    about brands, and pinning the legal name into those queries reliably returns
    filings and directory listings instead of the commentary being sought.

    **Every query is anchored to the UK**, added after the live trial returned a
    Better Business Bureau complaints page for "Monzo, Inc." — a US entity — in
    response to the reputation query. The subject of every report is a company
    on the UK register, and a brand name alone does not say which jurisdiction's
    legal entity is meant. The anchor makes that less likely, and is not trusted
    to make it impossible: `sources.names_a_different_entity` flags what still
    gets through, and Phase 4.2's prompts have to act on the flag.
    """
    short = trading_name(profile.name)
    return {
        WebSection.BUSINESS_MODEL: (
            f"What does {short} ({profile.name}) do in the UK — business model, "
            "products, services, and how the company makes money"
        ),
        WebSection.COMPETITIVE_LANDSCAPE: (
            f"{short} UK competitors, market position and competitive landscape"
        ),
        WebSection.REPUTATION: (
            f"{short} UK customer reviews, complaints, reputation and regulatory action"
        ),
    }


class SectionSearch(BaseModel):
    """What one section's search returned, and what it cost."""

    section: WebSection
    query: str
    depth: SearchDepth
    results: list[SearchResult] = Field(default_factory=list)
    credits_spent: int = 0


class WebGather(BaseModel):
    """Everything the web produced for one company, plus what was missed.

    Results stay grouped by section rather than being flattened into one list.
    Phase 4.2 uses different extraction framing per source type and Phase 5.4
    scores per section, so a flat list would have to be re-partitioned by both
    consumers — and the partition is only recoverable if it is kept here.
    """

    company_name: str
    sections: list[SectionSearch] = Field(default_factory=list)
    failures: list[GatherFailure] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def is_complete(self) -> bool:
        return not self.failures

    @property
    def missing(self) -> list[str]:
        return [failure.source for failure in self.failures]

    @property
    def credits_spent(self) -> int:
        return sum(section.credits_spent for section in self.sections)

    @property
    def total_results(self) -> int:
        return sum(len(section.results) for section in self.sections)

    def results_for(self, section: WebSection) -> list[SearchResult]:
        return [r for s in self.sections if s.section is section for r in s.results]


def _classify(section: WebSection, error: BaseException) -> GatherFailure:
    """Turn one search failure into a stated gap.

    Exhaustion is kept distinct from unavailability rather than collapsed into
    it. They read the same in a report — "no web evidence for this section" —
    but Phase 2.4 must respond differently: an outage is worth a cooldown and a
    later retry, whereas being out of credits will not resolve on its own, and
    retrying only spends the run's time budget discovering that again.
    """
    if isinstance(error, TavilyQuotaExhausted):
        return GatherFailure(source=section.value, kind=FailureKind.EXHAUSTED, detail=str(error))
    if isinstance(error, CircuitOpenError):
        # Skipped without calling out. Reported as unavailable, with the reason
        # in the detail — see the matching note in `registry_gather._classify`.
        return GatherFailure(source=section.value, kind=FailureKind.UNAVAILABLE, detail=str(error))
    if isinstance(error, TavilyUnavailable):
        return GatherFailure(
            source=section.value,
            kind=FailureKind.UNAVAILABLE,
            detail=str(error),
            retry_after=error.retry_after,
        )
    return GatherFailure(
        source=section.value,
        kind=FailureKind.ERROR,
        detail=f"{type(error).__name__}: {error}",
    )


async def gather_web(profile: CompanyProfile, client: TavilyClient) -> WebGather:
    """Run all three section searches concurrently for a resolved company.

    Takes the profile rather than a raw query string for the same reason the
    register gather does: it cannot then be called with something that never
    went through Phase 1's resolution, so no report is ever built about a
    company nobody confirmed.

    Raises only `TavilyAuthError`. Every other failure — including running out
    of credits — comes back inside the bundle as a stated gap, because a report
    that silently omits a section is the one outcome this whole layer exists to
    prevent.
    """
    queries = build_queries(profile)
    sections = list(queries)
    breaker = get_breaker(TAVILY)

    def under_breaker(section: WebSection):
        return guarded(
            breaker,
            # Bound as a default argument, not captured: a bare closure over the
            # loop variable would send all three searches whichever query the
            # comprehension finished on.
            lambda s=section: client.search(queries[s], depth=SECTION_DEPTH[s]),
            is_transient=is_tavily_transient,
            what=f"{section.value} search",
        )

    results = await asyncio.gather(
        *(under_breaker(section) for section in sections),
        return_exceptions=True,
    )

    # Checked across all three first: a rejected key would otherwise be recorded
    # as three separate section gaps in a report that should not exist at all.
    for result in results:
        if isinstance(result, TavilyAuthError):
            raise result

    gather = WebGather(company_name=profile.name)

    for section, result in zip(sections, results, strict=True):
        if isinstance(result, BaseException):
            failure = _classify(section, result)
            logger.warning(
                "web gather %s: %s search failed (%s) — %s",
                profile.number,
                section.value,
                failure.kind.value,
                failure.detail,
            )
            gather.failures.append(failure)
            continue

        gather.sections.append(
            SectionSearch(
                section=section,
                query=result.query,
                depth=result.depth,
                results=result.results,
                credits_spent=result.credits_spent,
            )
        )

    logger.info(
        "web gather %s: %d result(s) across %d section(s), %d credit(s), %d gap(s)",
        profile.number,
        gather.total_results,
        len(gather.sections),
        gather.credits_spent,
        len(gather.failures),
    )
    return gather
