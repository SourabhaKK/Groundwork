"""Extracted evidence claims (Phase 4.1), built early because Phase 3.5 needs them.

TASKS.md's implementation-order note requires this: 3.5's conditional edge
evaluates per-section claim sufficiency, so the schema it evaluates has to exist
before the edge can be wired or tested. The extraction *prompts* (4.2) and
conflict *detection* (4.3) stay in Phase 4 — this module defines only the shapes
they will produce and the granularity rule they must follow.

**Claim granularity** (TASKS.md 4.1) is the load-bearing convention here, and it
is a convention rather than something the type system can enforce: one claim is
one atomic, independently-citable fact. Bundling three facts from one FCA notice
into one claim, or splitting one fact into three by rephrasing, both move a
section's confidence band without any change in the underlying evidence. The
rule is stated on `EvidenceClaim` where an implementer of 4.2 will actually read
it, and worked through in `docs/phase-4.1-claim-granularity-example.md`.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, computed_field

from src.data_sources.sources import SourceType
from src.data_sources.web_gather import WebSection


class ClaimConfidence(StrEnum):
    """How well the *source* supports this one claim.

    Distinct from a section's `ConfidenceBand`: this is extraction's judgement
    about a single fact ("the document states this outright" vs. "this is
    implied"), whereas the band is a scored property of a whole section. Keeping
    them as separate types stops one being passed where the other is meant.
    """

    DIRECT = "direct"  # the source states it outright
    IMPLIED = "implied"  # a reasonable reading, not an explicit statement
    UNCERTAIN = "uncertain"  # retrieved, but the source is equivocal


class ConflictSeverity(StrEnum):
    """TASKS.md 5.4's definitions, as a type rather than a comment.

    MINOR: sources differ on phrasing or peripheral detail but agree on the core
    fact — two employee-count estimates, or two dates for one event inside a
    plausible range.

    MAJOR: sources contradict a core fact — one says acquired or dissolved while
    Companies House shows the company independently trading, or two sources give
    opposite signals on the same specific claim rather than merely differing in
    tone.
    """

    MINOR = "minor"
    MAJOR = "major"


class EvidenceClaim(BaseModel):
    """One atomic, independently-citable fact, with where it came from.

    Every field needed to cite it is required at construction, for the same
    reason `TaggedDocument` requires them: a claim that cannot say where it came
    from cannot be built at all, which makes traceability structural rather than
    a prompting convention that erodes.

    **Granularity rule — read before writing a 4.2 extraction prompt:**

    - *Don't bundle.* An FCA notice stating a fine amount AND a restriction
      breach AND a remediation status is three claims, not one, even though all
      three cite the same document.
    - *Don't split.* Rephrasing one fact, or citing the same sentence twice, is
      one claim. Over-fragmenting inflates the count without adding evidence and
      would let the confidence rule be gamed — accidentally or otherwise.
    - *Test:* if two people extracting from the same source would likely differ
      by more than one on how many facts it holds, the rule needs tightening
      with a concrete example from the source type causing the disagreement.
    """

    text: str = Field(min_length=1)
    source_url: str = Field(min_length=1)
    source_type: SourceType
    section: WebSection
    confidence: ClaimConfidence = ClaimConfidence.DIRECT

    # Phase 4.1. False when a claim is worth surfacing in prose but is not
    # reliable enough to score — a single secondary source with no independent
    # corroboration, or a figure substituted from an aggregator because the
    # primary returned an access error.
    #
    # The claim still renders in the report's prose and evidence-gap notes.
    # ONLY the Phase 5.4 scoring math excludes it. Dropping such claims from the
    # report entirely would be a different, worse decision: it would hide the
    # very thinness the band is reporting.
    counts_toward_confidence: bool = True
    exclusion_reason: str | None = None

    # Carried from `TaggedDocument.entity_warning`. Phase 4.2 must either refuse
    # to create claims from a warned document or mark them non-scoring with the
    # mismatch stated in `text` — never absorb them silently, since a complaint
    # against a foreign affiliate rendered as a fact about the UK company being
    # underwritten is a correctness failure, not a nuance.
    entity_warning: str | None = None

    origin: str = ""  # which document or endpoint produced it, for the 3.4 log
    extracted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def id(self) -> str:
        """Stable identity from content, not a random UUID.

        Conflicts reference claims by id, and the same evidence re-extracted in
        Phase 3.5's second pass must produce the same id or round two would
        duplicate every claim from round one — silently inflating claim counts,
        which is precisely what moves a confidence band without new evidence.
        A random id would also make every test assertion order-dependent.
        """
        digest = hashlib.sha256(f"{self.source_url}\n{self.text}".encode())
        return digest.hexdigest()[:12]


class Conflict(BaseModel):
    """Two or more claims that disagree (Phase 4.3 detects these).

    Surfaced, never silently resolved — the brief treats disagreement between
    sources as information the underwriter needs, not noise to be cleaned up
    before they see it.
    """

    severity: ConflictSeverity
    section: WebSection
    claim_ids: list[str] = Field(min_length=2)
    detail: str


class ExtractionResult(BaseModel):
    """What one extraction pass produced.

    Claims and conflicts travel together because Phase 5.4 needs both to score a
    section, and a caller that had to fetch them from two places could score
    against a stale half.
    """

    claims: list[EvidenceClaim] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)


def merge_claims(
    existing: list[EvidenceClaim], incoming: list[EvidenceClaim]
) -> list[EvidenceClaim]:
    """Combine two extraction passes, de-duplicating on claim identity.

    Phase 3.5 re-extracts after a targeted re-search, and the same document can
    legitimately be returned again by the follow-up query. Without this, round
    two would double-count round one's claims and lift a section's band on
    evidence it already had — the exact way a re-search could flatter itself.
    """
    merged = list(existing)
    seen = {claim.id for claim in existing}
    for claim in incoming:
        if claim.id not in seen:
            merged.append(claim)
            seen.add(claim.id)
    return merged


__all__ = [
    "ClaimConfidence",
    "Conflict",
    "ConflictSeverity",
    "EvidenceClaim",
    "ExtractionResult",
    "merge_claims",
]
