"""Graph state and the decision log (Phase 3.1, 3.2, 3.4).

**Pydantic, not `TypedDict`.** LangGraph's own idiom is a `TypedDict` state, and
it is lighter. AGENTS.md's rule that every structured object is a Pydantic model
wins here: this object is written by six nodes and read by the API layer, and
validation at each write is worth more than the ceremony it costs.

**Parallel writes.** The two gather branches run concurrently and write disjoint
keys — `registry` and `web` — so they need no merge reducer. The one field that
genuinely takes concurrent writes is `decisions`, which every node appends to;
it carries an `operator.add` reducer so two branches finishing at once append
rather than overwrite.

**The stopping condition (3.2) lives here as data**, not as a comparison buried
in a node. It is the rule the graph is supposed to be auditable against, so it
should be readable in one place without tracing control flow.
"""

from __future__ import annotations

import operator
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

from src.agent.claims import Conflict, EvidenceClaim
from src.agent.resolve import Resolution
from src.data_sources.companies_house import CompanyProfile
from src.data_sources.registry_gather import RegistryGather
from src.data_sources.sources import EvidenceBundle
from src.data_sources.tavily import SearchDepth
from src.data_sources.web_gather import (
    BASELINE_CREDITS_PER_REPORT,
    SECTION_DEPTH,
    WebGather,
    WebSection,
)
from src.report.confidence import ConfidenceAssessment

# ---------------------------------------------------------------------------
# Phase 3.2 — the stopping condition, stated as numbers rather than described
# ---------------------------------------------------------------------------

#: Initial gather, plus at most one re-search pass. The bound that makes 3.5 a
#: single adaptive retry rather than an iterative refinement loop.
MAX_GATHER_ROUNDS = 2

#: At most one re-search per under-evidenced section, and there are three
#: sections. Per-section rather than per-report because confidence is scored per
#: section: re-searching only one of three thin sections would leave the other
#: two reporting LOW while budget remained.
MAX_RESEARCH_SECTIONS = 3

#: Worst case per report: the baseline gather, then at most the same again if
#: all three sections need re-searching. `config.global_report_budget` is worked
#: back from this number.
MAX_SEARCH_CREDITS = 2 * BASELINE_CREDITS_PER_REPORT


def research_depth(section: WebSection) -> SearchDepth:
    """Re-search a section at the same depth its first pass used.

    Originally this was basic for every section, on the reasoning that the gap
    is a coverage problem — a whole angle missed — rather than a depth problem.
    A live Phase 3 trial disproved that. Given a good refined query aimed at
    regulators ("Monzo Bank Limited FCA complaints data UK Financial Ombudsman
    Service outcomes"), basic depth returned Wikipedia, the company homepage and
    two app-store listings. The *same query* at advanced depth returned the FCA
    firm-level complaints table, the FCA final notice imposing a £21m fine, and
    a Financial Ombudsman decision PDF — five results, none of them found by
    basic.

    That is precisely what Phase 2.2 recorded when it chose the first-pass
    depths: for competitive landscape and reputation, top hits are aggregator
    homepages whose basic snippet is generic boilerplate, and depth is what
    returns the actual text. That reason does not stop applying just because it
    is the second search rather than the first — so the depth decision is
    inherited rather than re-made, and a re-search cannot be cheaper than the
    pass it is trying to rescue.
    """
    return SECTION_DEPTH[section]


class RunOutcome(StrEnum):
    """How a run ended.

    `NEEDS_INPUT` is a success, not a failure: an ambiguous name correctly
    identified as ambiguous is the disambiguation feature working, and the API
    must not render it as an error.

    `PROVIDER_FAILURE` is kept distinct from `NO_EVIDENCE` for the reason
    `src/config.py` exists at all — "our LLM providers are down" must never
    reach the user wearing the same clothes as "this company is thinly
    documented".
    """

    COMPLETE = "complete"
    NEEDS_INPUT = "needs_input"
    NOT_FOUND = "not_found"
    INVALID_INPUT = "invalid_input"
    NO_EVIDENCE = "no_evidence"
    PROVIDER_FAILURE = "provider_failure"


class AgentDecision(BaseModel):
    """One decision the agent made, and why (Phase 3.4).

    Carried on state and returned by the API — not only written to `logging`.
    Cloud Run's logs are not part of the deliverable, and "Agent design" is
    graded on whether the reasoning is inspectable by whoever is reading the
    output.

    `rationale` is the field that matters. A log of *what* happened is a trace;
    this is meant to record *why*, including the times the agent decided to do
    nothing.
    """

    step: str
    action: str
    rationale: str
    outcome: str = ""
    detail: dict[str, object] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def line(self) -> str:
        text = f"[{self.step}] {self.action} — {self.rationale}"
        return f"{text} → {self.outcome}" if self.outcome else text


def decision(
    step: str, action: str, rationale: str, outcome: str = "", **detail: object
) -> AgentDecision:
    """Terse constructor, because nodes build these constantly."""
    return AgentDecision(
        step=step, action=action, rationale=rationale, outcome=outcome, detail=detail
    )


class ReportState(BaseModel):
    """Everything one report run accumulates."""

    query: str

    resolution: Resolution | None = None
    profile: CompanyProfile | None = None

    registry: RegistryGather | None = None
    web: WebGather | None = None
    evidence: EvidenceBundle | None = None

    claims: list[EvidenceClaim] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    assessments: dict[WebSection, ConfidenceAssessment] = Field(default_factory=dict)

    # Phase 3.2 counters. `gather_rounds` bounds the loop; `credits_spent` and
    # `researched_sections` bound its cost.
    gather_rounds: int = 0
    credits_spent: int = 0
    researched_sections: list[WebSection] = Field(default_factory=list)

    report: str | None = None
    outcome: RunOutcome | None = None
    error: str | None = None

    # The one field written concurrently, hence the reducer.
    decisions: Annotated[list[AgentDecision], operator.add] = Field(default_factory=list)

    @property
    def credits_remaining(self) -> int:
        return max(0, MAX_SEARCH_CREDITS - self.credits_spent)

    def may_research(self) -> bool:
        """Whether the 3.2 budget still permits any re-search at all.

        Three independent bounds, all of which must hold. Kept as one method so
        the conditional edge asks one question and the answer is auditable in
        one place.
        """
        return (
            self.gather_rounds < MAX_GATHER_ROUNDS
            and len(self.researched_sections) < MAX_RESEARCH_SECTIONS
            # The cheapest search that could still be run. Whether a *particular*
            # section is affordable at its own depth is checked in the research
            # node, which knows which sections it is about to run.
            and self.credits_remaining >= SearchDepth.BASIC.credits
        )

    def budget_note(self) -> str:
        """Why the budget said yes or no, for the decision log."""
        return (
            f"round {self.gather_rounds}/{MAX_GATHER_ROUNDS}, "
            f"{len(self.researched_sections)}/{MAX_RESEARCH_SECTIONS} sections re-searched, "
            f"{self.credits_remaining} of {MAX_SEARCH_CREDITS} credits left"
        )


__all__ = [
    "MAX_GATHER_ROUNDS",
    "MAX_RESEARCH_SECTIONS",
    "MAX_SEARCH_CREDITS",
    "AgentDecision",
    "ReportState",
    "RunOutcome",
    "decision",
    "research_depth",
]
