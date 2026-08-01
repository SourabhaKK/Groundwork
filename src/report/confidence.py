"""The uncertainty-scoring rule (TASKS.md 5.4), built in Phase 3 because 3.5 needs it.

This is deliberately an explicit, testable function rather than a prompt asking
a model how confident it feels. The first LLM council round found that every
planning document asserted an uncertainty mechanism while none specified one;
the fix was to write the rule down. Re-deriving it inside a prompt would undo
that fix, so the rule lives here and the model is never asked.

**Phase 3.5 uses the same function**, not a parallel heuristic. Two different
sufficiency tests would drift, and the moment they disagreed the agent would be
re-searching sections that score fine, or proceeding on sections it will later
mark LOW. `ConfidenceAssessment.re_search_worthwhile` is the only addition, and
it is a property of the reason, not a second rule.

**Precedence is load-bearing.** Conditions are evaluated strictly top to bottom
and the first match returns. An earlier draft listed HIGH before the
MINOR-conflict downgrade, which let any section passing HIGH's count-and-
authority test short-circuit before the downgrade was ever reached — making that
clause dead code in exactly the case the Phase 0.7 mock hit (7 claims, an
official source, one real minor conflict). The order below is the fix.

**Every count-based condition inherits Phase 4.1's granularity rule.** An
extraction step that bundles or over-fragments moves bands with no change in the
underlying evidence, and nothing in this module can detect that.
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum

from pydantic import BaseModel

from src.agent.claims import Conflict, ConflictSeverity, EvidenceClaim
from src.data_sources.sources import SourceType

# The claim count HIGH requires. Named because 3.5's re-search threshold and
# 5.4's HIGH gate are the same number by design, and a future change to one that
# silently missed the other would decouple "worth re-searching" from "would
# score badly" — which is the whole basis for the re-search decision.
HIGH_BAND_MIN_CLAIMS = 3


class ConfidenceBand(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ConfidenceReason(StrEnum):
    """Which condition in the rule actually fired.

    The band alone is not enough for two consumers. Phase 5.5 has to render
    *why* a section scored as it did rather than printing a bare badge, and
    Phase 3.5 has to distinguish "under-evidenced" from "contradicted" — they
    are both LOW, and only one of them is worth spending a search credit on.
    """

    NO_CLAIMS = "no_claims"
    MAJOR_CONFLICT = "major_conflict"
    WEAK_SOURCES = "weak_sources"
    MINOR_CONFLICT = "minor_conflict"
    CORROBORATED = "corroborated"
    SUFFICIENT = "sufficient"


# LOW reasons that more evidence could plausibly fix. A MAJOR conflict is
# excluded on purpose: the section is contradicted, not thin, and Phase 4.3's
# rule is that conflicts surface rather than get resolved. Spending a credit
# trying to out-vote a contradiction would work against that.
#
# Honest tradeoff, worth stating in the design note rather than hiding: more
# evidence *would* sometimes adjudicate a conflict, and this deliberately
# declines to try.
_RECOVERABLE = frozenset({ConfidenceReason.NO_CLAIMS, ConfidenceReason.WEAK_SOURCES})

_AUTHORITATIVE = frozenset({SourceType.OFFICIAL_REGISTRY, SourceType.TRADE_PRESS})


class ConfidenceAssessment(BaseModel):
    """A band, why it was reached, and what it was reached on."""

    band: ConfidenceBand
    reason: ConfidenceReason
    scored_claims: int
    total_claims: int

    @property
    def re_search_worthwhile(self) -> bool:
        """Whether Phase 3.5 should spend a search credit on this section."""
        return self.band is ConfidenceBand.LOW and self.reason in _RECOVERABLE

    def describe(self) -> str:
        """One line for the 3.4 decision log and the Phase 5.5 badge."""
        return (
            f"{self.band.value.upper()} ({self.reason.value}, "
            f"{self.scored_claims} scored of {self.total_claims} claims)"
        )


def _conflicts_among(
    conflicts: Sequence[Conflict], scored_ids: set[str], severity: ConflictSeverity
) -> bool:
    """Whether a conflict of this severity exists *between claims that score*.

    A conflict is only counted when **every** claim it references is scored.
    Phase 4.1 is explicit that an excluded claim still renders in prose and only
    leaves the scoring math — so a disagreement involving one is already
    reported to the reader, and letting it also drag the band down would apply
    the exclusion in one direction only.
    """
    return any(
        conflict.severity is severity
        and conflict.claim_ids
        and all(claim_id in scored_ids for claim_id in conflict.claim_ids)
        for conflict in conflicts
    )


def _meets_authority_gate(scored: Sequence[EvidenceClaim]) -> bool:
    """HIGH's source-authority test, including the `official_self` carve-out.

    An independent regulator and a company's own account of itself are not
    equally authoritative, which is why Phase 2.3a split `official` into
    `official_registry` and `official_self`. A self-disclosure counts toward
    this gate only when the section also holds a non-self claim: corroboration
    is the requirement, not agreement.
    """
    types = {claim.source_type for claim in scored}
    if types & _AUTHORITATIVE:
        return True
    return SourceType.OFFICIAL_SELF in types and bool(types - {SourceType.OFFICIAL_SELF})


def score_confidence(
    claims: Sequence[EvidenceClaim],
    conflicts: Sequence[Conflict] = (),
) -> ConfidenceAssessment:
    """Score one section. Runs per-section, never once per report.

    Evidence density varies by section — Companies House data is usually strong
    for business model and often weak for quality signals — so a single
    whole-report band would be an average that describes neither.
    """
    scored = [claim for claim in claims if claim.counts_toward_confidence]
    scored_ids = {claim.id for claim in scored}
    total = len(claims)

    def assess(band: ConfidenceBand, reason: ConfidenceReason) -> ConfidenceAssessment:
        return ConfidenceAssessment(
            band=band, reason=reason, scored_claims=len(scored), total_claims=total
        )

    if not scored:
        return assess(ConfidenceBand.LOW, ConfidenceReason.NO_CLAIMS)

    if _conflicts_among(conflicts, scored_ids, ConflictSeverity.MAJOR):
        return assess(ConfidenceBand.LOW, ConfidenceReason.MAJOR_CONFLICT)

    types = {claim.source_type for claim in scored}
    only_general_web = types == {SourceType.GENERAL_WEB}
    lone_review = len(scored) == 1 and types == {SourceType.REVIEW}
    if only_general_web or lone_review:
        return assess(ConfidenceBand.LOW, ConfidenceReason.WEAK_SOURCES)

    # Capped here regardless of count or authority. A minor conflict is evidence
    # the point isn't settled, and that cannot be outvoted by having gathered
    # more sources — which is what checking HIGH first would have allowed.
    if _conflicts_among(conflicts, scored_ids, ConflictSeverity.MINOR):
        return assess(ConfidenceBand.MEDIUM, ConfidenceReason.MINOR_CONFLICT)

    if len(scored) >= HIGH_BAND_MIN_CLAIMS and _meets_authority_gate(scored):
        return assess(ConfidenceBand.HIGH, ConfidenceReason.CORROBORATED)

    return assess(ConfidenceBand.MEDIUM, ConfidenceReason.SUFFICIENT)


__all__ = [
    "HIGH_BAND_MIN_CLAIMS",
    "ConfidenceAssessment",
    "ConfidenceBand",
    "ConfidenceReason",
    "score_confidence",
]
