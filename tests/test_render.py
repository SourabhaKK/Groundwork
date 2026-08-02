"""Tests for deterministic report rendering (Phases 5.5, 5.6) and the 5.7 comparator.

The property most of these exist to protect is narrow and specific: **every
number the report states about its own confidence comes from the one
`ConfidenceAssessment` the graph scored on.** The badge and the working table
are the two places a claim count appears, and if either recounted the claim list
independently they could disagree while both looked correct — which is exactly
the failure Phase 7.1a's second criterion is written to catch. Several tests
below therefore hand the renderer an assessment whose numbers deliberately do
*not* match the claim list, and assert the report prints the assessment's.

Nothing here calls a model. The renderer is a pure function of data, which is
the whole reason the model-writes-prose/code-writes-structure split was chosen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.agent.claims import Conflict, ConflictSeverity, EvidenceClaim
from src.data_sources.failures import FailureKind, GatherFailure
from src.data_sources.sources import SourceType, TaggedDocument
from src.data_sources.web_gather import WebSection
from src.report.citations import build_citations, markers_in, validate_markers
from src.report.confidence import (
    ConfidenceAssessment,
    ConfidenceBand,
    ConfidenceReason,
    score_confidence,
)
from src.report.render import SECTION_ORDER, SectionRender, render_report, render_section
from src.report.signature import structural_signature

BUSINESS = WebSection.BUSINESS_MODEL
COMPETITIVE = WebSection.COMPETITIVE_LANDSCAPE
REPUTATION = WebSection.REPUTATION

MOCK = Path(__file__).resolve().parents[1] / "docs" / "phase-0.7-mock-report-monzo.md"


def claim(
    text: str,
    *,
    url: str = "https://find-and-update.company-information.service.gov.uk/company/09446231",
    source_type: SourceType = SourceType.OFFICIAL_REGISTRY,
    section: WebSection = BUSINESS,
    scores: bool = True,
    exclusion_reason: str | None = None,
    entity_warning: str | None = None,
) -> EvidenceClaim:
    return EvidenceClaim(
        text=text,
        source_url=url,
        source_type=source_type,
        section=section,
        counts_toward_confidence=scores,
        exclusion_reason=exclusion_reason,
        entity_warning=entity_warning,
    )


def document(url: str, *, title: str = "A source") -> TaggedDocument:
    return TaggedDocument(
        url=url,
        source_type=SourceType.OFFICIAL_REGISTRY,
        retrieved_at=datetime(2026, 8, 2, tzinfo=UTC),
        content="content",
        title=title,
    )


def build(
    section: WebSection = BUSINESS,
    *,
    claims: list[EvidenceClaim] | None = None,
    assessment: ConfidenceAssessment | None = None,
    prose: str = "Prose about the company [1].",
    gaps: list[str] | None = None,
    conflicts: list[Conflict] | None = None,
    failures: list[GatherFailure] | None = None,
    documents: list[TaggedDocument] | None = None,
) -> SectionRender:
    claims = claims if claims is not None else [claim("A fact.")]
    return SectionRender(
        section=section,
        assessment=assessment or score_confidence(claims, conflicts or []),
        claims=claims,
        citations=build_citations(claims, documents or []),
        prose=prose,
        gaps=gaps or [],
        conflicts=conflicts or [],
        failures=failures or [],
    )


def full_report(renders: list[SectionRender]) -> str:
    return render_report(
        company_name="MONZO BANK LIMITED",
        company_number="09446231",
        generated_at=datetime(2026, 8, 2, tzinfo=UTC),
        resolution_line="Exact match on registered name. No disambiguation required.",
        renders=renders,
    )


# ---------------------------------------------------------------------------
# The one-number-one-source discipline
# ---------------------------------------------------------------------------


def test_the_badge_prints_the_assessments_counts_not_a_recount() -> None:
    """The badge must not measure the claim list it was handed.

    The assessment here says 7 claims / 6 scored while the list holds 2. That
    combination cannot occur in a real run — it is constructed precisely so a
    renderer that recounted would be caught, because a recount would print 2/2.
    """
    claims = [claim("One."), claim("Two.")]
    assessment = ConfidenceAssessment(
        band=ConfidenceBand.MEDIUM,
        reason=ConfidenceReason.MINOR_CONFLICT,
        scored_claims=6,
        total_claims=7,
    )
    rendered = render_section(build(claims=claims, assessment=assessment), 1)

    assert "7 claims (6 scored)" in rendered
    assert "2 claims" not in rendered


def test_the_working_table_reads_the_same_object_the_badge_does() -> None:
    """The report's other claim-count statement, held to the same rule.

    This is the specific discipline confirmed before the live pass: the working
    table at the bottom must not recompute what the badge above it already
    stated, or the two can disagree while each looks internally consistent.
    """
    claims = [claim("One."), claim("Two.")]
    assessment = ConfidenceAssessment(
        band=ConfidenceBand.HIGH,
        reason=ConfidenceReason.CORROBORATED,
        scored_claims=5,
        total_claims=9,
    )
    report = full_report(
        [build(section, claims=claims, assessment=assessment) for section in SECTION_ORDER]
    )

    working = report.split("Confidence Scoring Applied")[1]
    for line in working.splitlines():
        if line.startswith("| 1.") or line.startswith("| 2.") or line.startswith("| 3."):
            cells = [cell.strip() for cell in line.split("|")]
            assert cells[2] == "9", line  # Claims
            assert cells[3] == "5", line  # Scored
    assert report.count("9 claims (5 scored)") == 3


def test_the_working_table_shows_precedence_rather_than_every_condition() -> None:
    """Conditions after the one that fired were never evaluated, and say so."""
    claims = [claim("One.", scores=False, exclusion_reason="unverifiable quote")]
    report = full_report([build(claims=claims)] + [build(section) for section in SECTION_ORDER[1:]])

    row = next(line for line in report.splitlines() if line.startswith("| 1."))
    assert "(not reached)" in row
    assert "🔴 LOW" in row


# ---------------------------------------------------------------------------
# Rendering what the rule produced, including its known rough edges
# ---------------------------------------------------------------------------


def test_a_major_conflict_renders_as_low_with_the_disagreement_shown() -> None:
    """Phase 4 disclosed that a denominator mismatch can be misread as MAJOR.

    The renderer's job is to print what `score_confidence` returned, including
    that case — not to second-guess it. The conflict is shown so a reader can
    judge the misclassification for themselves, which is what makes the
    disclosure honest rather than buried.
    """
    claims = [claim("40% use it as a main account."), claim("49% of actives use it as primary.")]
    conflict = Conflict(
        severity=ConflictSeverity.MAJOR,
        section=BUSINESS,
        claim_ids=[claims[0].id, claims[1].id],
        detail="One source says 40%, the other 49%.",
    )
    rendered = render_section(build(claims=claims, conflicts=[conflict]), 1)

    assert "🔴 LOW" in rendered
    assert "MAJOR conflict between scored claims" in rendered
    assert "⚠️ Conflict detected — MAJOR (drives the band)" in rendered
    assert "One source says 40%, the other 49%." in rendered


def test_a_conflict_that_did_not_move_the_band_still_renders_and_says_so() -> None:
    """Phase 4 carried this forward explicitly: an excluded claim's disagreement
    still reaches the reader, and the badge must not imply otherwise."""
    scoring = [claim("A."), claim("B."), claim("C.")]
    excluded = claim("D.", scores=False, exclusion_reason="single uncorroborated source")
    conflict = Conflict(
        severity=ConflictSeverity.MINOR,
        section=BUSINESS,
        claim_ids=[scoring[0].id, excluded.id],
        detail="They differ on a peripheral detail.",
    )
    rendered = render_section(build(claims=[*scoring, excluded], conflicts=[conflict]), 1)

    assert "🟢 HIGH" in rendered
    assert "⚠️ Conflict detected — MINOR (recorded; did not move the band)" in rendered


def test_excluded_claims_are_listed_with_their_reasons() -> None:
    claims = [
        claim("A scoring fact."),
        claim(
            "~700,000 business customers.",
            url="https://whitesight.net/x",
            source_type=SourceType.GENERAL_WEB,
            scores=False,
            exclusion_reason="single secondary source with no independent corroboration",
        ),
    ]
    rendered = render_section(build(claims=claims), 1)

    assert "**Excluded from scoring (`counts_toward_confidence = False`):**" in rendered
    assert "~700,000 business customers." in rendered
    assert "single secondary source with no independent corroboration" in rendered
    assert "[2]" in rendered


def test_gather_failures_render_as_stated_gaps_alongside_the_models_gaps() -> None:
    """Phase 2 made gathering partial-tolerant on the condition its failures get
    rendered. A bundle whose failures are dropped is worse than one that failed
    loudly."""
    failure = GatherFailure(
        source="officers", kind=FailureKind.UNAVAILABLE, detail="Companies House returned 503"
    )
    rendered = render_section(
        build(gaps=["No lending-book size was verifiable."], failures=[failure]), 1
    )

    assert "⚠️ Evidence gaps" in rendered
    assert "No lending-book size was verifiable." in rendered
    assert (
        "**officers could not be retrieved** (unavailable): Companies House returned 503"
        in rendered
    )


def test_a_section_with_no_claims_renders_honestly_rather_than_vanishing() -> None:
    rendered = render_section(build(claims=[], prose="No usable evidence was retrieved."), 2)

    assert "## 2." in rendered
    assert "🔴 LOW" in rendered
    assert "0 claims (0 scored)" in rendered
    assert "No source produced a usable claim for this section." in rendered


def test_quality_signals_carries_an_explicit_signal_strength_rating() -> None:
    """TASKS.md 5.3, derived from the band rather than judged separately."""
    claims = [
        claim("A.", section=REPUTATION),
        claim("B.", section=REPUTATION),
        claim("C.", section=REPUTATION),
    ]
    rendered = render_section(build(REPUTATION, claims=claims), 3)

    assert "Signal strength: strong." in rendered


# ---------------------------------------------------------------------------
# Shape: the Phase 0.7 mock's skeleton
# ---------------------------------------------------------------------------


def test_the_report_has_no_top_summary_table_and_no_trailing_disclaimer() -> None:
    report = full_report([build(section) for section in SECTION_ORDER])
    head = report.split("## 1.")[0]

    assert "|" not in head, "a summary table above section 1 is exactly what the mock omits"
    signature = structural_signature(report)
    assert signature.trailing_blocks == ()
    assert signature.has_working_table


def test_sections_render_in_the_mocks_order_with_its_headings() -> None:
    report = full_report([build(section) for section in SECTION_ORDER])
    signature = structural_signature(report)

    assert [s.title for s in signature.sections] == [
        "Business Model Summary",
        "Competitive Landscape Assessment",
        "Company Quality Signals",
    ]
    assert all(section.has_badge and section.has_sources_table for section in signature.sections)


def test_the_source_table_tags_every_row_with_its_authority_tier() -> None:
    claims = [
        claim("A."),
        claim("B.", url="https://www.uktech.news/x", source_type=SourceType.TRADE_PRESS),
    ]
    docs = [document(claims[0].source_url), document(claims[1].source_url, title="UKTN")]
    rendered = render_section(build(claims=claims, documents=docs), 1)

    assert "`official_registry`" in rendered
    assert "`trade_press`" in rendered
    assert "02 Aug 2026" in rendered


# ---------------------------------------------------------------------------
# Citations (5.6)
# ---------------------------------------------------------------------------


def test_citations_number_by_first_appearance_and_collapse_repeat_sources() -> None:
    url_a, url_b = "https://a.example/x", "https://b.example/y"
    claims = [
        claim("One.", url=url_a),
        claim("Two.", url=url_b),
        claim("Three.", url=url_a),
    ]
    citations = build_citations(claims)

    assert len(citations) == 2
    assert citations.number_for(url_a) == 1
    assert citations.number_for(url_b) == 2


def test_a_source_that_produced_no_claim_never_reaches_the_table() -> None:
    """The claim list decides what is cited — not the document list.

    A document that was gathered but yielded nothing is a gap, not a source.
    Listing it would imply evidence the report does not actually have.
    """
    claims = [claim("One.", url="https://a.example/x")]
    docs = [document("https://a.example/x"), document("https://unused.example/z")]
    citations = build_citations(claims, docs)

    assert [c.url for c in citations.citations] == ["https://a.example/x"]


def test_an_out_of_range_marker_is_stripped_and_reported() -> None:
    """The prompt asks the model to carry markers through; a prompt is a request.

    `[5]` in a two-source section is a miscount or an invention, and both render
    as a citation an underwriter would click and not find.
    """
    cleaned, warnings = validate_markers("Revenue rose [1]. Profit fell [5].", {1, 2})

    assert cleaned == "Revenue rose [1]. Profit fell."
    assert len(warnings) == 1
    assert "[5]" in warnings[0]


def test_a_valid_marker_survives_untouched() -> None:
    cleaned, warnings = validate_markers("Revenue rose [1][2].", {1, 2})

    assert cleaned == "Revenue rose [1][2]."
    assert warnings == []
    assert markers_in(cleaned) == {1, 2}


# ---------------------------------------------------------------------------
# The Phase 5.7 / 7.1 comparator itself
# ---------------------------------------------------------------------------


def test_two_reports_differing_only_in_prose_have_the_same_signature() -> None:
    """The property Phase 5.7 rests on: prose is free, shape is not."""
    left = full_report([build(section, prose="One phrasing [1].") for section in SECTION_ORDER])
    right = full_report(
        [
            build(section, prose="A completely different phrasing of it [1].")
            for section in SECTION_ORDER
        ]
    )

    assert structural_signature(left).matches(structural_signature(right))


def test_the_signature_catches_a_missing_section() -> None:
    full = full_report([build(section) for section in SECTION_ORDER])
    short = full_report([build(section) for section in SECTION_ORDER[:2]])

    deltas = structural_signature(full).differences(structural_signature(short))
    assert any("section count" in delta for delta in deltas)


def test_the_signature_catches_a_dropped_citation() -> None:
    """A prose difference that is not merely cosmetic: a factual sentence with
    no citation fails Phase 7.1a's first criterion."""
    claims = [claim("A.", url="https://a.example/x"), claim("B.", url="https://b.example/y")]
    cited = full_report(
        [build(claims=claims, prose="Both facts [1][2].")] + [build(s) for s in SECTION_ORDER[1:]]
    )
    uncited = full_report(
        [build(claims=claims, prose="Both facts [1].")] + [build(s) for s in SECTION_ORDER[1:]]
    )

    deltas = structural_signature(cited).differences(structural_signature(uncited))
    assert any("citation markers" in delta for delta in deltas)


@pytest.mark.skipif(not MOCK.exists(), reason="the Phase 0.7 mock is not in this checkout")
def test_the_comparator_reads_the_phase_0_7_mock() -> None:
    """The comparator has to work on the hand-written target, not only on our own
    output — otherwise Phase 7.1's structural diff would be self-referential."""
    signature = structural_signature(MOCK.read_text(encoding="utf-8"))

    assert [s.title for s in signature.sections] == [
        "Business Model Summary",
        "Competitive Landscape Assessment",
        "Company Quality Signals",
    ]
    assert [s.band for s in signature.sections] == ["MEDIUM", "HIGH", "HIGH"]
    assert [s.source_rows for s in signature.sections] == [4, 4, 3]
    assert all(s.has_exclusion_note and s.has_gaps_block for s in signature.sections)
    assert signature.has_working_table and signature.working_table_rows == 3
