"""The real `/report` endpoint (Phase 6.1), behind the Phase 0.5 gate.

The gate, the rate limiter and the request model were built in Phase 0 and are
unchanged here — the whole point of building them first was that this endpoint
arrives already constrained rather than being exempted until the end.

**Three decisions this module rests on, all approved before it was written.**

*It does not stream.* TASKS.md 6.1 offers "streams progress if feasible". The
value streaming would buy — showing the agent reasoning as it happens — is
already delivered, and delivered better, by `decisions`: an auditable record of
every judgement including the ones where the agent decided to do nothing. A
progress bar is a worse version of that. Deferred deliberately, not overlooked.

*Only a provider failure is an HTTP error.* Every other outcome is a 200
carrying a typed `outcome`. `NEEDS_INPUT` is the disambiguation feature working
and `NOT_FOUND` is a finding about a company, not a malformed request; both
would be mis-rendered by a client that reads status codes. `PROVIDER_FAILURE`
alone gets a 503, for the reason `src/config.py` exists at all — "our LLM
providers are down" must never reach a reader wearing the same clothes as "this
company is thinly documented".

*Dependencies are built per request.* Both source clients own an
`httpx.AsyncClient`; Phase 1 recorded that one cannot be closed from a
synchronous reset hook, which is why there is no module singleton to reuse. Two
TCP handshakes against a 60-90s graph run is not a cost worth reintroducing that
lifetime problem for.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TypeAlias

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from src.agent.claims import Conflict, EvidenceClaim
from src.agent.deps import AgentDependencies
from src.agent.graph import run_report
from src.agent.resolve import Candidate, ConfirmationReason, Resolution, ResolutionKind
from src.agent.state import AgentDecision, ReportState, RunOutcome
from src.api.auth import require_token
from src.api.rate_limit import enforce_rate_limit
from src.data_sources.companies_house import CompaniesHouseClient
from src.data_sources.tavily import TavilyClient

logger = logging.getLogger(__name__)

router = APIRouter()

#: One query in, the final graph state out. Everything the endpoint does with
#: that state is a pure projection, which is what makes it testable.
ReportRunner: TypeAlias = Callable[[str], Awaitable[ReportState]]


class ReportRequest(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="Company name or Companies House registration number.",
    )


class CandidateOut(BaseModel):
    """One disambiguation candidate.

    Carries enough for a human to actually choose — status, SIC codes and
    incorporation date are what distinguish two similarly-named companies — plus
    `number`, which is what the client sends back to resolve the choice.
    """

    number: str
    name: str
    status: str | None = None
    incorporated_on: str | None = None
    sic_codes: list[str] = Field(default_factory=list)
    address: str | None = None
    match: str
    matched_previous_name: str | None = None


class ResolutionOut(BaseModel):
    kind: ResolutionKind
    detail: str | None = None
    reasons: list[ConfirmationReason] = Field(default_factory=list)
    candidates: list[CandidateOut] = Field(default_factory=list)


class AssessmentOut(BaseModel):
    """A band as the graph scored it.

    `summary` is `ConfidenceAssessment.describe()` — the same string the report's
    badge prints from. Nothing here is recomputed: Phase 5's whole architecture
    is that a band is produced once and rendered everywhere, and a client that
    re-derived one could disagree with the report it is displayed next to.
    """

    band: str
    reason: str
    scored_claims: int
    total_claims: int
    summary: str


class ClaimOut(BaseModel):
    text: str
    source_url: str
    source_type: str
    section: str
    confidence: str
    counts_toward_confidence: bool
    exclusion_reason: str | None = None
    entity_warning: str | None = None


class ConflictOut(BaseModel):
    severity: str
    section: str
    detail: str
    claim_ids: list[str]


class DecisionOut(BaseModel):
    step: str
    action: str
    rationale: str
    outcome: str = ""
    detail: dict[str, object] = Field(default_factory=dict)
    at: str


class ReportResponse(BaseModel):
    """What one run produced.

    The rendered markdown is the headline, but it is not the whole deliverable.
    `decisions` is the evidence for the agent-design criterion, `assessments` is
    the uncertainty mechanism stated as data rather than as prose, and `claims`
    lets a reader check any citation without parsing the report.
    """

    outcome: RunOutcome
    query: str
    company_name: str | None = None
    company_number: str | None = None
    report: str | None = None
    resolution: ResolutionOut | None = None
    assessments: dict[str, AssessmentOut] = Field(default_factory=dict)
    claims: list[ClaimOut] = Field(default_factory=list)
    conflicts: list[ConflictOut] = Field(default_factory=list)
    decisions: list[DecisionOut] = Field(default_factory=list)
    credits_spent: int = 0
    gather_rounds: int = 0
    error: str | None = None


def _candidate_out(candidate: Candidate) -> CandidateOut:
    # `one_line()` already exists on the address for this exact purpose — Phase 1
    # wrote it to show an underwriter which candidate is which.
    profile = candidate.profile
    address = profile.registered_office_address
    return CandidateOut(
        number=profile.number,
        name=profile.name,
        status=profile.status,
        incorporated_on=profile.incorporated_on.isoformat() if profile.incorporated_on else None,
        sic_codes=list(profile.sic_codes),
        address=address.one_line() if address else None,
        match=candidate.match,
        matched_previous_name=candidate.matched_previous_name,
    )


def _resolution_out(resolution: Resolution | None) -> ResolutionOut | None:
    if resolution is None:
        return None
    return ResolutionOut(
        kind=resolution.kind,
        detail=resolution.detail,
        reasons=list(resolution.reasons),
        candidates=[_candidate_out(c) for c in resolution.candidates],
    )


def _claim_out(claim: EvidenceClaim) -> ClaimOut:
    return ClaimOut(
        text=claim.text,
        source_url=claim.source_url,
        source_type=claim.source_type.value,
        section=claim.section.value,
        confidence=claim.confidence.value,
        counts_toward_confidence=claim.counts_toward_confidence,
        exclusion_reason=claim.exclusion_reason,
        entity_warning=claim.entity_warning,
    )


def _conflict_out(conflict: Conflict) -> ConflictOut:
    return ConflictOut(
        severity=conflict.severity.value,
        section=conflict.section.value,
        detail=conflict.detail,
        claim_ids=list(conflict.claim_ids),
    )


def _decision_out(entry: AgentDecision) -> DecisionOut:
    return DecisionOut(
        step=entry.step,
        action=entry.action,
        rationale=entry.rationale,
        outcome=entry.outcome,
        detail=entry.detail,
        at=entry.at.isoformat(),
    )


def to_response(state: ReportState) -> ReportResponse:
    """Project final graph state onto the wire format.

    A pure function of state, separated from the endpoint so it is testable
    without a graph run and so no field is derived from anything but what the
    run actually produced.
    """
    profile = state.profile
    return ReportResponse(
        # A state that reached here without an outcome is a graph bug, not a
        # user-visible condition; NO_EVIDENCE is the honest floor rather than
        # inventing a success.
        outcome=state.outcome or RunOutcome.NO_EVIDENCE,
        query=state.query,
        company_name=getattr(profile, "name", None),
        company_number=getattr(profile, "number", None),
        report=state.report,
        resolution=_resolution_out(state.resolution),
        assessments={
            section.value: AssessmentOut(
                band=assessment.band.value,
                reason=assessment.reason.value,
                scored_claims=assessment.scored_claims,
                total_claims=assessment.total_claims,
                summary=assessment.describe(),
            )
            for section, assessment in state.assessments.items()
        },
        claims=[_claim_out(c) for c in state.claims],
        conflicts=[_conflict_out(c) for c in state.conflicts],
        decisions=[_decision_out(d) for d in state.decisions],
        credits_spent=state.credits_spent,
        gather_rounds=state.gather_rounds,
        error=state.error,
    )


async def run_with_real_dependencies(query: str) -> ReportState:
    """One run against the real world, with both clients closed afterwards."""
    async with CompaniesHouseClient() as companies_house, TavilyClient() as tavily:
        deps = AgentDependencies.build(companies_house=companies_house, tavily=tavily)
        return await run_report(query, deps)


def get_report_runner() -> ReportRunner:
    """The seam that keeps this endpoint testable without a network.

    Added after the first version of this module was written without one — and
    the test suite caught it immediately, by making a real Companies House call
    from a unit test and getting a live 401. AGENTS.md forbids exactly that, and
    CI runs without secrets specifically so it fails there. Overriding this
    dependency is how a test drives the endpoint's status-code and projection
    logic against a `ReportState` it built by hand.
    """
    return run_with_real_dependencies


@router.post(
    "/report",
    response_model=ReportResponse,
    dependencies=[Depends(require_token), Depends(enforce_rate_limit)],
    summary="Generate an underwriting intelligence report for a UK company",
)
async def create_report(
    request: ReportRequest,
    runner: ReportRunner = Depends(get_report_runner),
) -> ReportResponse:
    state = await runner(request.query)
    response = to_response(state)

    if response.outcome is RunOutcome.PROVIDER_FAILURE:
        # The one outcome that is genuinely our failure rather than a finding
        # about the company. 503 so a client can distinguish it without reading
        # the body, and so it never renders as an unusually thin report.
        logger.error("provider failure serving %r: %s", request.query, state.error)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Report generation could not complete: all model providers failed. "
                "This is a service fault, not a finding about the company."
            ),
        )

    return response


__all__ = [
    "ReportRequest",
    "ReportResponse",
    "ReportRunner",
    "get_report_runner",
    "router",
    "run_with_real_dependencies",
    "to_response",
]
