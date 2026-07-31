"""The Companies House deep pull (Phase 2.1).

Takes a company that Phase 1 already resolved and fetches everything else the
register holds about it — filing history, officers, persons with significant
control — concurrently, into one bundle.

**This gather is partial-tolerant, and that is a deliberate departure from
Phase 1.** `resolve._hydrate` is all-or-nothing because resolution makes a
uniqueness claim: "this is the only exact active match" is only true if every
candidate was actually seen, so ranking on the survivors of a partial fetch
would quietly weaken the claim without changing how it reads. Gathering makes
no such claim. It accumulates evidence, and a report built on filings and PSC
with the officers list missing is still a perfectly honest report *provided it
says the officers list is missing*.

That proviso is the whole design. Every failure is recorded as a `GatherFailure`
carrying which source failed and why, because the project's worst failure mode —
named in BUILD_LOG since Phase 0 — is a broken fetch rendering as thin evidence,
indistinguishable from an honestly thin company. Silence is the bug; a stated
gap is the feature. Phase 5 must render `failures`, and Phase 5.4 must not score
an absent source as an absent fact.

Two failures are not degraded:

- **`CompaniesHouseAuthError` is re-raised.** A rejected key is our
  misconfiguration, not a fact about the company. Degrading it would print "no
  officers recorded" for every company we ever look at, which is both false and
  self-concealing — the same reasoning `resolve._hydrate` applies.
- **A 404 on the PSC endpoint is not a failure at all.** The register genuinely
  returns 404 when it holds no PSC record, so this is a finding — nobody is
  recorded as controlling this company — and it renders as an empty list with
  no gap noted. On filings or officers the same status *is* recorded, because
  the profile provably exists and those endpoints should have answered.

No retry, no backoff, no sleeping happens here. A `Retry-After` from the
register is carried onto the failure record for Phase 2.4 to act on, in the same
spirit as Phase 1.1 surfacing it rather than consuming it — one retry policy for
every source, decided in one place, instead of two that drift apart.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field

from src.data_sources.companies_house import (
    FILING_HISTORY_LIMIT,
    CompaniesHouseAuthError,
    CompaniesHouseClient,
    CompaniesHouseUnavailable,
    CompanyNotFound,
    CompanyProfile,
    FilingRecord,
    Officer,
    PersonWithSignificantControl,
)
from src.data_sources.failures import FailureKind, GatherFailure

logger = logging.getLogger(__name__)


class RegistrySource(StrEnum):
    """The three endpoints this gather pulls. Named so a failure record says
    which part of the register is missing, not merely that something is."""

    FILING_HISTORY = "filing_history"
    OFFICERS = "officers"
    PSC = "psc"


class RegistryGather(BaseModel):
    """Everything the register holds about one company, plus what was missed.

    `*_total` fields carry the register's own count next to the items we
    actually kept. A 100-filing cap on a company with 400 filings is otherwise
    indistinguishable from a company that has filed 100 times, and a report
    describing a truncated history as the company's whole record would be wrong
    in a way nothing downstream could detect.
    """

    profile: CompanyProfile
    filings: list[FilingRecord] = Field(default_factory=list)
    filings_total: int | None = None
    officers: list[Officer] = Field(default_factory=list)
    officers_total: int | None = None
    psc: list[PersonWithSignificantControl] = Field(default_factory=list)
    psc_total: int | None = None
    failures: list[GatherFailure] = Field(default_factory=list)
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @property
    def company_number(self) -> str:
        return self.profile.number

    @property
    def company_name(self) -> str:
        return self.profile.name

    @property
    def is_complete(self) -> bool:
        """True if all three sources answered. False is not an error — it is a
        fact the report is required to state."""
        return not self.failures

    @property
    def missing(self) -> list[str]:
        return [failure.source for failure in self.failures]

    @property
    def filings_truncated(self) -> bool:
        return self.filings_total is not None and self.filings_total > len(self.filings)


def _classify(source: RegistrySource, error: BaseException) -> GatherFailure:
    """Turn one exception into a stated gap, preserving the client's own split.

    The three-way classification `companies_house` established in Phase 1 is
    carried through rather than collapsed: "the register is down" and "we asked
    it something malformed" must stay distinguishable here, because 2.4's
    per-source breaker is about to trip on the first and must never trip on the
    second.
    """
    if isinstance(error, CompaniesHouseUnavailable):
        return GatherFailure(
            source=source,
            kind=FailureKind.UNAVAILABLE,
            detail=str(error),
            retry_after=error.retry_after,
        )
    if isinstance(error, CompanyNotFound):
        return GatherFailure(source=source, kind=FailureKind.NOT_FOUND, detail=str(error))
    return GatherFailure(
        source=source,
        kind=FailureKind.ERROR,
        detail=f"{type(error).__name__}: {error}",
    )


async def gather_companies_house(
    profile: CompanyProfile,
    client: CompaniesHouseClient,
    *,
    filing_limit: int = FILING_HISTORY_LIMIT,
) -> RegistryGather:
    """Pull filing history, officers and PSC for an already-resolved company.

    Takes the `CompanyProfile` rather than a company number, for two reasons:
    it makes it impossible to call this with a number that never went through
    resolution, and it means the profile is not re-fetched — Phase 1 already
    paid for it, and re-fetching would risk the bundle disagreeing with the
    company the user actually confirmed.

    The three calls run concurrently with `return_exceptions=True`, so one
    failing endpoint cannot cancel the other two in flight. Raises only
    `CompaniesHouseAuthError`; every other failure comes back inside the bundle.
    """
    number = profile.number

    results = await asyncio.gather(
        client.get_filing_history(number, limit=filing_limit),
        client.get_officers(number),
        client.get_psc(number),
        return_exceptions=True,
    )

    # Checked across all three before anything else: a rejected key would
    # otherwise be recorded three times as three separate "gaps" in a report
    # that should not have been produced at all.
    for result in results:
        if isinstance(result, CompaniesHouseAuthError):
            raise result

    gather = RegistryGather(profile=profile)
    sources = (RegistrySource.FILING_HISTORY, RegistrySource.OFFICERS, RegistrySource.PSC)

    for source, result in zip(sources, results, strict=True):
        if isinstance(result, BaseException):
            # No PSC record is the ordinary case for a great many companies, and
            # the register signals it with a 404. Recording that as a gap would
            # put a "data missing" caveat in the majority of reports and dilute
            # the ones where something genuinely did fail.
            if source is RegistrySource.PSC and isinstance(result, CompanyNotFound):
                logger.info("gather %s: no PSC record on the register", number)
                continue

            failure = _classify(source, result)
            logger.warning(
                "gather %s: %s unavailable (%s) — %s",
                number,
                source.value,
                failure.kind.value,
                failure.detail,
            )
            gather.failures.append(failure)
            continue

        items, total = result
        if source is RegistrySource.FILING_HISTORY:
            gather.filings, gather.filings_total = items, total
        elif source is RegistrySource.OFFICERS:
            gather.officers, gather.officers_total = items, total
        else:
            gather.psc, gather.psc_total = items, total

    logger.info(
        "gather %s: %d filings (of %s), %d officers, %d psc, %d source(s) missing",
        number,
        len(gather.filings),
        gather.filings_total,
        len(gather.officers),
        len(gather.psc),
        len(gather.failures),
    )
    return gather
