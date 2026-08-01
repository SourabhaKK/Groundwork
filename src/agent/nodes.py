"""The graph's nodes (Phase 3.1-3.5).

Each node takes the state and the injected dependencies and returns a *partial*
update, which LangGraph merges. Nodes never raise for an expected failure: the
gather layer already returns its failures inside the bundle, and Phase 5 has to
render them. A node that raised on a missing source would convert a report with
a stated gap — the honest outcome — into no report at all.

Every node appends at least one `AgentDecision`, including when it decides to do
nothing. A log that records only the times the agent acted cannot be audited for
judgement, which is the property Phase 3.4 exists to provide.
"""

from __future__ import annotations

import asyncio
import logging

from src.agent.claims import merge_claims
from src.agent.deps import AgentDependencies
from src.agent.research import QueryRefinement, refine_query
from src.agent.resolve import ResolutionKind, resolve_company
from src.agent.state import ReportState, RunOutcome, decision, research_depth
from src.circuit_breaker import CircuitOpenError
from src.data_sources.breakers import TAVILY, get_breaker, guarded, is_tavily_transient
from src.data_sources.companies_house import CompaniesHouseAuthError
from src.data_sources.registry_gather import gather_companies_house
from src.data_sources.sources import build_evidence
from src.data_sources.tavily import TavilyAuthError
from src.data_sources.web_gather import (
    SectionSearch,
    WebGather,
    WebSection,
    gather_web,
)
from src.llm.router import AllProvidersFailedError
from src.report.confidence import ConfidenceAssessment, score_confidence

logger = logging.getLogger(__name__)

# Node names, in one place. The routers below return these strings and the graph
# maps them; a typo in either would otherwise be a runtime KeyError deep in a
# LangGraph traversal rather than an import-time mistake.
RESOLVE = "resolve"
GATHER_REGISTRY = "gather_registry"
GATHER_WEB = "gather_web"
ASSEMBLE = "assemble_evidence"
EXTRACT = "extract"
EVALUATE = "evaluate_sufficiency"
RESEARCH = "research_gap"
SYNTHESIZE = "synthesize"
HALT = "halt"

_ALL_SECTIONS: tuple[WebSection, ...] = tuple(WebSection)

_HALTING_KINDS = {
    ResolutionKind.AMBIGUOUS: RunOutcome.NEEDS_INPUT,
    ResolutionKind.NEEDS_CONFIRMATION: RunOutcome.NEEDS_INPUT,
    ResolutionKind.NOT_FOUND: RunOutcome.NOT_FOUND,
    ResolutionKind.INVALID: RunOutcome.INVALID_INPUT,
}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


async def resolve_node(state: ReportState, deps: AgentDependencies) -> dict:
    """Identify the company, or stop.

    An auth failure is the one thing allowed to end the run as an error: it is
    not a fact about the company, and continuing would produce a report about
    nothing while looking like a report about a thinly-documented business.
    """
    try:
        resolution = await resolve_company(state.query, deps.companies_house)
    except CompaniesHouseAuthError as exc:
        return {
            "outcome": RunOutcome.PROVIDER_FAILURE,
            "error": f"Companies House rejected our credentials: {exc}",
            "decisions": [
                decision(
                    RESOLVE,
                    "resolve company",
                    "the register is the only source of company identity; without it "
                    "there is no subject to report on",
                    outcome="halted: Companies House auth failure",
                )
            ],
        }

    update: dict = {
        "resolution": resolution,
        "decisions": [
            decision(
                RESOLVE,
                f"resolve {state.query!r} against the register",
                "no report is built about a company nobody confirmed, so resolution "
                "gates everything downstream",
                outcome=f"{resolution.kind.value}"
                + (f" — {resolution.profile.name}" if resolution.profile else ""),
                candidates=len(resolution.candidates),
                reasons=[reason.value for reason in resolution.reasons],
            )
        ],
    }

    if resolution.kind is ResolutionKind.RESOLVED and resolution.profile is not None:
        update["profile"] = resolution.profile
    else:
        update["outcome"] = _HALTING_KINDS.get(resolution.kind, RunOutcome.NOT_FOUND)
        update["error"] = resolution.detail

    return update


def route_after_resolve(state: ReportState) -> list[str]:
    """Fan out to both gather branches, or stop.

    Returning a list is how the two source branches run concurrently: LangGraph
    schedules every name returned here. They write disjoint state keys, so no
    merge reducer is needed for their results.
    """
    if state.profile is None:
        return [HALT]
    return [GATHER_REGISTRY, GATHER_WEB]


# ---------------------------------------------------------------------------
# Gathering — two parallel branches
# ---------------------------------------------------------------------------


async def gather_registry_node(state: ReportState, deps: AgentDependencies) -> dict:
    assert state.profile is not None  # guaranteed by route_after_resolve
    try:
        gather = await gather_companies_house(state.profile, deps.companies_house)
    except CompaniesHouseAuthError as exc:
        return {
            "decisions": [
                decision(
                    GATHER_REGISTRY,
                    "pull filings, officers and PSC",
                    "register data is the spine of the report; its absence is stated, "
                    "never silently absorbed",
                    outcome=f"failed: {exc}",
                )
            ]
        }

    return {
        "registry": gather,
        "decisions": [
            decision(
                GATHER_REGISTRY,
                "pull filings, officers and PSC",
                "the register is the one source with no stake in what it reports, so it "
                "anchors every section",
                outcome=f"{len(gather.failures)} gap(s)"
                if gather.failures
                else "complete",
                missing=gather.missing,
            )
        ],
    }


async def gather_web_node(state: ReportState, deps: AgentDependencies) -> dict:
    assert state.profile is not None
    try:
        gather = await gather_web(state.profile, deps.tavily)
    except TavilyAuthError as exc:
        return {
            "decisions": [
                decision(
                    GATHER_WEB,
                    "run three section-targeted searches",
                    "web evidence carries the competitive and reputation sections, which "
                    "the register cannot speak to",
                    outcome=f"failed: {exc}",
                )
            ]
        }

    return {
        "web": gather,
        "credits_spent": gather.credits_spent,
        "decisions": [
            decision(
                GATHER_WEB,
                "run three section-targeted searches",
                "one query per section, because a single query returns a homogeneous "
                "blob that over-serves business model and starves quality signals",
                outcome=f"{gather.total_results} result(s), {gather.credits_spent} credit(s)",
                missing=gather.missing,
            )
        ],
    }


async def assemble_node(state: ReportState, deps: AgentDependencies) -> dict:
    """Fan-in: merge both gathers into one tagged bundle."""
    assert state.profile is not None
    evidence = build_evidence(state.profile, state.registry, state.web)

    update: dict = {
        "evidence": evidence,
        "gather_rounds": state.gather_rounds + 1,
        "decisions": [
            decision(
                ASSEMBLE,
                "tag and merge both sources into one evidence bundle",
                "documents and stated gaps travel together, so a missing source can "
                "never read as an absent fact",
                outcome=f"{len(evidence.documents)} document(s), {len(evidence.failures)} gap(s)",
                by_type={t.value: n for t, n in evidence.count_by_type().items()},
            )
        ],
    }

    if not evidence.documents:
        update["outcome"] = RunOutcome.NO_EVIDENCE
        update["error"] = "No evidence could be gathered from either source."

    return update


def route_after_assemble(state: ReportState) -> str:
    """Proceed on a partial bundle; stop only on a completely empty one.

    Partial is the normal case, not an edge case — one source reachable and the
    other not still produces a report, with the gap stated in it.
    """
    return HALT if state.outcome is RunOutcome.NO_EVIDENCE else EXTRACT


# ---------------------------------------------------------------------------
# Extraction and sufficiency
# ---------------------------------------------------------------------------


def _sections_to_extract(state: ReportState) -> tuple[WebSection, ...]:
    """All three on the first pass; only the re-searched ones on the second.

    Re-extracting all three after a targeted re-search would spend tokens
    re-deriving claims the state already holds, and `merge_claims` would discard
    them as duplicates anyway.
    """
    if state.researched_sections:
        return tuple(state.researched_sections)
    return _ALL_SECTIONS


async def extract_node(state: ReportState, deps: AgentDependencies) -> dict:
    """Turn documents into claims (Phase 4 supplies the implementation)."""
    assert state.evidence is not None
    sections = _sections_to_extract(state)

    try:
        result = await deps.extractor(state.evidence, sections)
    except AllProvidersFailedError as exc:
        # Distinct from "no evidence" on purpose. An outage that rendered as a
        # thin report would be indistinguishable from an honest thin report —
        # the failure this whole project refuses to allow.
        return {
            "outcome": RunOutcome.PROVIDER_FAILURE,
            "error": f"Every LLM provider was unavailable during extraction: {exc}",
            "decisions": [
                decision(
                    EXTRACT,
                    "extract claims from gathered documents",
                    "an extraction outage must be reported as an outage, never as an "
                    "absence of evidence about the company",
                    outcome="halted: all providers failed",
                )
            ],
        }

    claims = merge_claims(state.claims, result.claims)
    new_claims = len(claims) - len(state.claims)

    return {
        "claims": claims,
        "conflicts": state.conflicts + result.conflicts,
        "decisions": [
            decision(
                EXTRACT,
                f"extract claims for {', '.join(s.value for s in sections)}",
                "one claim is one atomic, independently-citable fact — the granularity "
                "the confidence rule counts on",
                outcome=f"{new_claims} new claim(s), {len(claims)} total"
                + (" [placeholder extractor]" if deps.extraction_is_placeholder else ""),
                placeholder=deps.extraction_is_placeholder,
            )
        ],
    }


def _assess(state: ReportState) -> dict[WebSection, ConfidenceAssessment]:
    return {
        section: score_confidence(
            [claim for claim in state.claims if claim.section is section],
            [conflict for conflict in state.conflicts if conflict.section is section],
        )
        for section in _ALL_SECTIONS
    }


async def evaluate_node(state: ReportState, deps: AgentDependencies) -> dict:
    """Score every section, and record the re-search decision for each (3.5).

    The negative decisions are logged as deliberately as the positive ones. A
    reader has to be able to see that a section scoring MEDIUM was considered
    and passed over, rather than never having been looked at.
    """
    assessments = _assess(state)
    budget_allows = state.may_research()
    note = state.budget_note()

    decisions = []
    for section, assessment in assessments.items():
        if section in state.researched_sections:
            verdict = "already re-searched once — the loop is bounded at one pass"
        elif not assessment.re_search_worthwhile:
            verdict = (
                "contradicted, not thin — more evidence cannot unmake a conflict, and "
                "Phase 4.3 requires conflicts to surface rather than be resolved"
                if assessment.band.value == "low"
                else "sufficiently evidenced"
            )
        elif not budget_allows:
            verdict = f"under-evidenced, but the gather budget is spent ({note})"
        else:
            verdict = "under-evidenced and recoverable — re-search"

        decisions.append(
            decision(
                EVALUATE,
                f"score {section.value}",
                "the same score_confidence() the report will publish, so the agent "
                "cannot re-search on a rule the reader never sees",
                outcome=f"{assessment.describe()} → {verdict}",
                section=section.value,
                band=assessment.band.value,
                reason=assessment.reason.value,
                scored_claims=assessment.scored_claims,
                re_search=verdict.startswith("under-evidenced and recoverable"),
            )
        )

    return {"assessments": assessments, "decisions": decisions}


def sections_needing_research(state: ReportState) -> list[WebSection]:
    """Which sections the conditional edge will actually act on."""
    if not state.may_research():
        return []
    return [
        section
        for section, assessment in state.assessments.items()
        if assessment.re_search_worthwhile and section not in state.researched_sections
    ]


def route_after_evaluate(state: ReportState) -> str:
    """Phase 3.5's conditional edge — the graph's one data-dependent branch.

    This is what makes the system an agent rather than a pipeline: which node
    runs next is decided by what the evidence turned out to be, not by position
    in a fixed order. It is bounded by `may_research()` rather than by structure,
    so the bound is auditable as data.
    """
    return RESEARCH if sections_needing_research(state) else SYNTHESIZE


# ---------------------------------------------------------------------------
# Phase 3.5 — the bounded, targeted re-search
# ---------------------------------------------------------------------------


def _gap_description(state: ReportState, section: WebSection) -> str:
    assessment = state.assessments[section]
    seen = len([c for c in state.claims if c.section is section])
    if assessment.scored_claims == 0:
        return f"no usable claims at all for {section.value.replace('_', ' ')}"
    return (
        f"only {assessment.scored_claims} scored claim(s) of {seen}, and they are "
        f"{assessment.reason.value.replace('_', ' ')}"
    )


def _seen_urls(state: ReportState, section: WebSection) -> list[str]:
    if state.evidence is None:
        return []
    return [document.url for document in state.evidence.for_section(section)]


async def research_node(state: ReportState, deps: AgentDependencies) -> dict:
    """One targeted follow-up search per under-evidenced section.

    Capped at `MAX_RESEARCH_SECTIONS`, basic depth, through the same Tavily
    breaker and against the same credit budget as the first pass. Re-searching
    outside the breaker would reintroduce doomed calls after exhaustion, which
    is the failure Phase 2.4 exists to prevent.
    """
    assert state.profile is not None
    targets = sections_needing_research(state)
    breaker = get_breaker(TAVILY)

    refinements: list[QueryRefinement] = await asyncio.gather(
        *(
            refine_query(
                section=section,
                profile=state.profile,
                gap=_gap_description(state, section),
                seen_urls=_seen_urls(state, section),
                router=deps.router,
            )
            for section in targets
        )
    )

    def under_breaker(refinement: QueryRefinement):
        depth = research_depth(refinement.section)
        return guarded(
            breaker,
            # Bound as a default argument rather than captured, for the same
            # reason `gather_web` does it: a bare closure over the loop variable
            # would send every re-search whichever query the comprehension
            # finished on.
            lambda r=refinement, d=depth: deps.tavily.search(r.query, depth=d),
            is_transient=is_tavily_transient,
            what=f"{refinement.section.value} re-search",
        )

    searches = await asyncio.gather(
        *(under_breaker(refinement) for refinement in refinements),
        return_exceptions=True,
    )

    web = state.web or WebGather(company_name=state.profile.name)
    sections = list(web.sections)
    failures = list(web.failures)
    decisions = []
    researched = list(state.researched_sections)
    credits = 0

    for refinement, result in zip(refinements, searches, strict=True):
        researched.append(refinement.section)

        if isinstance(result, BaseException):
            reason = (
                "circuit open — not calling out"
                if isinstance(result, CircuitOpenError)
                else f"{type(result).__name__}: {result}"
            )
            decisions.append(
                decision(
                    RESEARCH,
                    f"re-search {refinement.section.value}",
                    refinement.targets_gap
                    or "the first pass left this section under-evidenced",
                    outcome=f"no results — {reason}",
                    **_refinement_detail(refinement),
                )
            )
            continue

        credits += result.credits_spent
        sections.append(
            SectionSearch(
                section=refinement.section,
                query=result.query,
                depth=result.depth,
                results=result.results,
                credits_spent=result.credits_spent,
            )
        )
        decisions.append(
            decision(
                RESEARCH,
                f"re-search {refinement.section.value}",
                refinement.targets_gap
                or "the first pass left this section under-evidenced",
                outcome=f"{len(result.results)} result(s), {result.credits_spent} credit(s)",
                **_refinement_detail(refinement),
            )
        )

    refreshed = WebGather(
        company_name=web.company_name,
        sections=sections,
        failures=failures,
        retrieved_at=web.retrieved_at,
    )

    return {
        "web": refreshed,
        "evidence": build_evidence(state.profile, state.registry, refreshed),
        "credits_spent": state.credits_spent + credits,
        "researched_sections": researched,
        "gather_rounds": state.gather_rounds + 1,
        "decisions": decisions,
    }


def _refinement_detail(refinement: QueryRefinement) -> dict[str, object]:
    """The three things TASKS.md requires the log to capture for a re-search:
    that it fired, the reasoning, and the refined query itself — alongside the
    original, so a reader can see for themselves that they differ."""
    return {
        "section": refinement.section.value,
        "original_query": refinement.original,
        "refined_query": refinement.query,
        "query_source": refinement.source.value,
        "targets_gap": refinement.targets_gap,
        "differs_because": refinement.differs_because,
        "rejected_query": refinement.rejected_query,
        "rejection_reason": refinement.rejection_reason,
    }


# ---------------------------------------------------------------------------
# Synthesis and termination
# ---------------------------------------------------------------------------


async def synthesize_node(state: ReportState, deps: AgentDependencies) -> dict:
    """Render the report (Phase 5 supplies the implementation)."""
    try:
        report = await deps.synthesizer(state)
    except AllProvidersFailedError as exc:
        return {
            "outcome": RunOutcome.PROVIDER_FAILURE,
            "error": f"Every LLM provider was unavailable during synthesis: {exc}",
            "decisions": [
                decision(
                    SYNTHESIZE,
                    "write the three sections from extracted claims",
                    "a provider outage is reported as an outage, never as a thin report",
                    outcome="halted: all providers failed",
                )
            ],
        }

    bands = ", ".join(
        f"{section.value}={assessment.band.value}"
        for section, assessment in state.assessments.items()
    )
    return {
        "report": report,
        "outcome": RunOutcome.COMPLETE,
        "decisions": [
            decision(
                SYNTHESIZE,
                "write the three sections from extracted claims only",
                "synthesis is grounded in the claim list, so nothing can enter the "
                "report without a source already attached to it",
                outcome=f"report rendered ({bands})",
            )
        ],
    }


async def halt_node(state: ReportState, deps: AgentDependencies) -> dict:
    """Terminal for every non-report outcome.

    Exists as a real node so that stopping is visible in the graph rather than
    being an early return hidden inside another node.
    """
    outcome = state.outcome or RunOutcome.NOT_FOUND
    return {
        "outcome": outcome,
        "decisions": [
            decision(
                HALT,
                "stop before synthesis",
                "an ambiguous or unresolvable subject is a result to report, not an "
                "error to swallow — and never a report about the wrong company",
                outcome=outcome.value,
            )
        ],
    }


__all__ = [
    "ASSEMBLE",
    "EVALUATE",
    "EXTRACT",
    "GATHER_REGISTRY",
    "GATHER_WEB",
    "HALT",
    "RESEARCH",
    "RESOLVE",
    "SYNTHESIZE",
    "assemble_node",
    "evaluate_node",
    "extract_node",
    "gather_registry_node",
    "gather_web_node",
    "halt_node",
    "research_node",
    "resolve_node",
    "route_after_assemble",
    "route_after_evaluate",
    "route_after_resolve",
    "sections_needing_research",
    "synthesize_node",
]
