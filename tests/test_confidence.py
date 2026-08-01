"""The Phase 5.4 scoring rule.

Tested exhaustively and early because two separate things depend on it being
exactly right: the band printed next to every section of the report, and Phase
3.5's decision about whether to spend a search credit. A bug here is not a
cosmetic one — it either publishes a confidence claim the evidence does not
support, or sends the agent researching sections that were fine.

The precedence tests matter most. An earlier draft of the rule checked HIGH
before the MINOR-conflict downgrade, which made the downgrade unreachable for
any section that passed HIGH's count-and-authority test — dead code in exactly
the case the Phase 0.7 mock hit.
"""

from __future__ import annotations

import pytest

from src.agent.claims import (
    ClaimConfidence,
    Conflict,
    ConflictSeverity,
    EvidenceClaim,
)
from src.data_sources.sources import SourceType
from src.data_sources.web_gather import WebSection
from src.report.confidence import (
    ConfidenceBand,
    ConfidenceReason,
    score_confidence,
)

SECTION = WebSection.BUSINESS_MODEL


def claim(
    text: str,
    source_type: SourceType = SourceType.TRADE_PRESS,
    *,
    url: str | None = None,
    scored: bool = True,
) -> EvidenceClaim:
    return EvidenceClaim(
        text=text,
        source_url=url or f"https://example.com/{abs(hash(text)) % 10_000}",
        source_type=source_type,
        section=SECTION,
        confidence=ClaimConfidence.DIRECT,
        counts_toward_confidence=scored,
    )


def conflict(*claims: EvidenceClaim, severity: ConflictSeverity) -> Conflict:
    return Conflict(
        severity=severity,
        section=SECTION,
        claim_ids=[c.id for c in claims],
        detail="test conflict",
    )


# ---------------------------------------------------------------------------
# The LOW conditions
# ---------------------------------------------------------------------------


def test_no_claims_is_low() -> None:
    assessment = score_confidence([])
    assert assessment.band is ConfidenceBand.LOW
    assert assessment.reason is ConfidenceReason.NO_CLAIMS


def test_claims_that_all_fail_to_score_are_the_same_as_none() -> None:
    """`counts_toward_confidence=False` removes a claim from the maths only.

    The claim still exists and still renders in prose — this asserts the scoring
    half, that a section carrying only unscoreable claims is honestly LOW rather
    than borrowing confidence from claims it already admitted were unreliable.
    """
    claims = [claim(f"unreliable {i}", scored=False) for i in range(5)]

    assessment = score_confidence(claims)

    assert assessment.band is ConfidenceBand.LOW
    assert assessment.reason is ConfidenceReason.NO_CLAIMS
    assert assessment.scored_claims == 0
    assert assessment.total_claims == 5  # still counted, still renderable


def test_only_general_web_is_low_however_many() -> None:
    claims = [claim(f"blog says {i}", SourceType.GENERAL_WEB) for i in range(6)]

    assessment = score_confidence(claims)

    assert assessment.band is ConfidenceBand.LOW
    assert assessment.reason is ConfidenceReason.WEAK_SOURCES


def test_a_single_review_claim_is_low() -> None:
    assessment = score_confidence([claim("one review", SourceType.REVIEW)])
    assert assessment.band is ConfidenceBand.LOW
    assert assessment.reason is ConfidenceReason.WEAK_SOURCES


def test_two_review_claims_are_not_the_single_review_case() -> None:
    """The rule says `len(scored) == 1 AND review`, so a second review escapes
    that clause. Pinned because it is the kind of condition that gets
    'simplified' into `any review` by someone reading quickly."""
    claims = [claim("review one", SourceType.REVIEW), claim("review two", SourceType.REVIEW)]

    assessment = score_confidence(claims)

    assert assessment.band is ConfidenceBand.MEDIUM


def test_major_conflict_is_low_regardless_of_everything_else() -> None:
    a = claim("acquired in 2025", SourceType.OFFICIAL_REGISTRY)
    b = claim("trading independently", SourceType.TRADE_PRESS)
    claims = [a, b, claim("third"), claim("fourth")]

    assessment = score_confidence(claims, [conflict(a, b, severity=ConflictSeverity.MAJOR)])

    assert assessment.band is ConfidenceBand.LOW
    assert assessment.reason is ConfidenceReason.MAJOR_CONFLICT


# ---------------------------------------------------------------------------
# Precedence — the bug this rule was rewritten to fix
# ---------------------------------------------------------------------------


def test_minor_conflict_caps_a_section_that_would_otherwise_be_high() -> None:
    """The Phase 0.7 mock's case: enough claims, an authoritative source, and one
    real minor conflict. If HIGH were checked first this would return HIGH and
    the downgrade would be unreachable."""
    a = claim("about 3.2m customers", SourceType.OFFICIAL_REGISTRY)
    b = claim("about 3.5m customers", SourceType.TRADE_PRESS)
    claims = [a, b, claim("third"), claim("fourth"), claim("fifth")]

    assessment = score_confidence(claims, [conflict(a, b, severity=ConflictSeverity.MINOR)])

    assert assessment.band is ConfidenceBand.MEDIUM
    assert assessment.reason is ConfidenceReason.MINOR_CONFLICT


def test_major_conflict_outranks_minor_conflict() -> None:
    a = claim("dissolved", SourceType.OFFICIAL_REGISTRY)
    b = claim("active", SourceType.TRADE_PRESS)
    c = claim("3.2m customers")
    d = claim("3.5m customers")

    assessment = score_confidence(
        [a, b, c, d],
        [
            conflict(c, d, severity=ConflictSeverity.MINOR),
            conflict(a, b, severity=ConflictSeverity.MAJOR),
        ],
    )

    assert assessment.reason is ConfidenceReason.MAJOR_CONFLICT


def test_a_conflict_involving_an_unscored_claim_does_not_downgrade() -> None:
    """Phase 4.1 says an excluded claim leaves the scoring maths and nothing
    else. Letting it still drag the band down would apply the exclusion in one
    direction only — removing its support while keeping its damage."""
    a = claim("solid figure", SourceType.OFFICIAL_REGISTRY)
    b = claim("dubious aggregator figure", SourceType.GENERAL_WEB, scored=False)

    assessment = score_confidence(
        [a, b, claim("second"), claim("third")],
        [conflict(a, b, severity=ConflictSeverity.MAJOR)],
    )

    assert assessment.band is ConfidenceBand.HIGH


# ---------------------------------------------------------------------------
# HIGH, and the official_self carve-out
# ---------------------------------------------------------------------------


def test_three_claims_with_a_registry_source_is_high() -> None:
    claims = [
        claim("incorporated 2015", SourceType.OFFICIAL_REGISTRY),
        claim("press coverage", SourceType.TRADE_PRESS),
        claim("more coverage", SourceType.TRADE_PRESS),
    ]

    assessment = score_confidence(claims)

    assert assessment.band is ConfidenceBand.HIGH
    assert assessment.reason is ConfidenceReason.CORROBORATED


def test_two_authoritative_claims_are_not_enough() -> None:
    claims = [
        claim("incorporated 2015", SourceType.OFFICIAL_REGISTRY),
        claim("press coverage", SourceType.TRADE_PRESS),
    ]

    assert score_confidence(claims).band is ConfidenceBand.MEDIUM


def test_self_disclosure_alone_never_reaches_high() -> None:
    """A company's own account of itself is primary but self-interested.
    Corroboration is the requirement, not agreement — so three self-authored
    claims stay MEDIUM however many there are."""
    claims = [claim(f"our annual report says {i}", SourceType.OFFICIAL_SELF) for i in range(4)]

    assessment = score_confidence(claims)

    assert assessment.band is ConfidenceBand.MEDIUM
    assert assessment.reason is ConfidenceReason.SUFFICIENT


def test_self_disclosure_counts_once_corroborated_by_a_non_self_claim() -> None:
    claims = [
        claim("our annual report", SourceType.OFFICIAL_SELF),
        claim("our annual report again", SourceType.OFFICIAL_SELF),
        claim("an independent blog", SourceType.GENERAL_WEB),
    ]

    assessment = score_confidence(claims)

    assert assessment.band is ConfidenceBand.HIGH


@pytest.mark.parametrize(
    "source_type",
    [SourceType.GENERAL_WEB, SourceType.REVIEW],
)
def test_three_weak_claims_with_no_authority_are_medium_not_high(
    source_type: SourceType,
) -> None:
    """Count alone must not reach HIGH — the authority gate is a separate test,
    and a section resting on three blogs is not the same as one resting on the
    register."""
    claims = [claim(f"weak {i}", source_type) for i in range(3)]
    claims.append(claim("a review", SourceType.REVIEW))

    assert score_confidence(claims).band is ConfidenceBand.MEDIUM


# ---------------------------------------------------------------------------
# What Phase 3.5 keys off
# ---------------------------------------------------------------------------


def test_thin_sections_are_worth_re_searching() -> None:
    assert score_confidence([]).re_search_worthwhile is True
    assert score_confidence([claim("blog", SourceType.GENERAL_WEB)]).re_search_worthwhile is True


def test_a_contradicted_section_is_not_worth_re_searching() -> None:
    """LOW, but not thin. More evidence cannot unmake a contradiction, and Phase
    4.3 requires conflicts to surface rather than be resolved — so the credit is
    better left unspent. This is the distinction `ConfidenceReason` exists for."""
    a = claim("dissolved", SourceType.OFFICIAL_REGISTRY)
    b = claim("active", SourceType.TRADE_PRESS)

    assessment = score_confidence([a, b], [conflict(a, b, severity=ConflictSeverity.MAJOR)])

    assert assessment.band is ConfidenceBand.LOW
    assert assessment.re_search_worthwhile is False


def test_well_evidenced_sections_are_not_re_searched() -> None:
    claims = [
        claim("incorporated 2015", SourceType.OFFICIAL_REGISTRY),
        claim("press", SourceType.TRADE_PRESS),
        claim("more press", SourceType.TRADE_PRESS),
    ]

    assert score_confidence(claims).re_search_worthwhile is False


def test_the_badge_states_claim_count_not_source_count() -> None:
    """Phase 0.7 found the mock's own badge conflating the two. The rule scores
    on claims, so the badge has to say claims."""
    claims = [claim("a", SourceType.OFFICIAL_REGISTRY), claim("b", scored=False)]

    described = score_confidence(claims).describe()

    assert "1 scored of 2 claims" in described
