"""Tests for report synthesis (Phases 5.1-5.3, 5.7).

The properties under test are the ones that would fail quietly:

1. **Synthesis runs on the SYNTHESIS tier.** The router picks the model from the
   tier, so a synthesis call that asked for EXTRACTION would silently run the
   report on Haiku and nothing would look wrong.
2. **The model is never shown a URL.** Same mechanism as Phase 4.2 one layer
   down: it cannot mint a citation it was never given a mapping for.
3. **A failing section degrades to a stated absence, not a missing section.** A
   dropped section takes its evidence gaps with it and shortens the report
   silently.
4. **Total provider failure propagates**, because an outage rendered as a thin
   report is indistinguishable from an honest report about a thin company.
5. **Two providers produce the same shape** — Phase 5.7, tested here under fakes
   and separately validated live against the real Gemini fallback (see
   `BUILD_LOG.md` Phase 5).

No live provider is called here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from src.agent.claims import Conflict, ConflictSeverity, EvidenceClaim
from src.agent.resolve import ConfirmationReason, Resolution, ResolutionKind
from src.agent.state import ReportState
from src.data_sources.companies_house import CompanyProfile
from src.data_sources.failures import FailureKind, GatherFailure
from src.data_sources.sources import EvidenceBundle, SourceType, TaggedDocument
from src.data_sources.web_gather import WebSection
from src.llm.router import AllProvidersFailedError, LLMResponse, LLMTier
from src.report.signature import structural_signature
from src.report.synthesize import ReportSynthesizer, resolution_line

BUSINESS = WebSection.BUSINESS_MODEL
COMPETITIVE = WebSection.COMPETITIVE_LANDSCAPE
REPUTATION = WebSection.REPUTATION

REGISTER = "https://find-and-update.company-information.service.gov.uk/company/09446231"
FCA = "https://www.fca.org.uk/news/press-releases/fca-fines-monzo-21m"


class FakeRouter:
    """Scripted on the prompt, not on call order — the three sections run
    concurrently, and an order-keyed fake would make these tests depend on
    scheduling."""

    def __init__(
        self,
        handler: Callable[[str], str | BaseException],
        *,
        used_fallback: bool = False,
        provider: str = "fake",
    ) -> None:
        self._handler = handler
        self._used_fallback = used_fallback
        self._provider = provider
        self.prompts: list[str] = []
        self.tiers: list[LLMTier] = []

    async def complete(
        self,
        prompt: str,
        *,
        tier: LLMTier,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        self.tiers.append(tier)
        result = self._handler(prompt)
        if isinstance(result, BaseException):
            raise result
        return LLMResponse(
            text=result,
            provider=self._provider,
            model="fake-model",
            used_fallback=self._used_fallback,
        )


def prose(text: str = "A grounded sentence [1].", gaps: list[str] | None = None) -> str:
    return json.dumps({"prose": text, "gaps": gaps if gaps is not None else ["A stated gap."]})


def claim(
    text: str,
    *,
    section: WebSection = BUSINESS,
    url: str = REGISTER,
    source_type: SourceType = SourceType.OFFICIAL_REGISTRY,
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


def state(
    *,
    claims: list[EvidenceClaim] | None = None,
    conflicts: list[Conflict] | None = None,
    failures: list[GatherFailure] | None = None,
) -> ReportState:
    claims = (
        claims
        if claims is not None
        else [
            claim("Incorporated 18 February 2015."),
            claim("Active on the register."),
            claim("Files under SIC 64191.", section=COMPETITIVE),
            claim("Fined GBP 21m by the FCA.", section=REPUTATION, url=FCA),
        ]
    )
    profile = CompanyProfile(company_number="09446231", company_name="MONZO BANK LIMITED")
    documents = [
        TaggedDocument(
            url=url,
            source_type=SourceType.OFFICIAL_REGISTRY,
            retrieved_at=datetime(2026, 8, 2, tzinfo=UTC),
            content="content",
            title="A source",
        )
        for url in {c.source_url for c in claims}
    ]
    return ReportState(
        query="Monzo",
        profile=profile,
        resolution=Resolution(kind=ResolutionKind.RESOLVED, query="Monzo", profile=profile),
        evidence=EvidenceBundle(
            company_number="09446231",
            company_name="MONZO BANK LIMITED",
            documents=documents,
            failures=failures or [],
        ),
        claims=claims,
        conflicts=conflicts or [],
    )


# ---------------------------------------------------------------------------
# Tiering and grounding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_every_synthesis_call_uses_the_synthesis_tier() -> None:
    """Extraction was Haiku; synthesis is Sonnet-only. The tier is how that is
    enforced, and a call on the wrong tier would look entirely normal."""
    router = FakeRouter(lambda _: prose())
    await ReportSynthesizer(router)(state())  # type: ignore[arg-type]

    assert router.tiers == [LLMTier.SYNTHESIS] * 3


@pytest.mark.asyncio
async def test_one_call_per_section() -> None:
    router = FakeRouter(lambda _: prose())
    await ReportSynthesizer(router)(state())  # type: ignore[arg-type]

    assert len(router.prompts) == 3
    assert {
        "Business Model Summary",
        "Competitive Landscape Assessment",
        "Company Quality Signals",
    } == {
        line.split("SECTION: ")[1].rstrip(".")
        for prompt in router.prompts
        for line in prompt.splitlines()
        if line.startswith("SECTION: ")
    }


@pytest.mark.asyncio
async def test_the_prompt_never_contains_a_source_url() -> None:
    """The model's only handle on a source is the marker already bound to the
    claim. With no URL in the prompt there is no mapping it could extend, which
    is what makes a fabricated citation unrepresentable rather than discouraged."""
    router = FakeRouter(lambda _: prose())
    await ReportSynthesizer(router)(state())  # type: ignore[arg-type]

    for prompt in router.prompts:
        assert "http://" not in prompt
        assert "https://" not in prompt
        assert "company-information.service.gov.uk" not in prompt


@pytest.mark.asyncio
async def test_the_prompt_states_the_band_the_prose_must_live_with() -> None:
    router = FakeRouter(lambda _: prose())
    await ReportSynthesizer(router)(state())  # type: ignore[arg-type]

    business = next(p for p in router.prompts if "SECTION: Business Model" in p)
    assert "Band: MEDIUM" in business
    assert "not yours to change" in business


@pytest.mark.asyncio
async def test_excluded_and_entity_mismatched_claims_reach_the_prompt_flagged() -> None:
    """Phase 4 carried both forward. An excluded claim must still be available to
    the prose — dropping it makes the report look thinner than the evidence
    gathered — and an entity-mismatched claim must never read as a fact about
    the company under report."""
    claims = [
        claim("A scoring fact."),
        claim(
            "~700,000 business customers.",
            url="https://whitesight.net/x",
            source_type=SourceType.GENERAL_WEB,
            scores=False,
            exclusion_reason="single secondary source with no independent corroboration",
        ),
        claim(
            '[Refers to "Monzo, Inc.", not MONZO BANK LIMITED] 30 complaints in 3 years.',
            url="https://www.bbb.org/x",
            source_type=SourceType.REVIEW,
            scores=False,
            entity_warning='names "Monzo, Inc."',
        ),
    ]
    router = FakeRouter(lambda _: prose())
    await ReportSynthesizer(router)(state(claims=claims))  # type: ignore[arg-type]

    business = next(p for p in router.prompts if "SECTION: Business Model" in p)
    assert "[EXCLUDED from scoring — single secondary source" in business
    assert "[ENTITY MISMATCH —" in business
    assert "~700,000 business customers." in business


@pytest.mark.asyncio
async def test_conflicts_are_given_as_report_both_sides_never_pick_a_winner() -> None:
    claims = [claim("40% main account."), claim("49% primary bank.")]
    conflict = Conflict(
        severity=ConflictSeverity.MAJOR,
        section=BUSINESS,
        claim_ids=[claims[0].id, claims[1].id],
        detail="40% versus 49%.",
    )
    router = FakeRouter(lambda _: prose())
    await ReportSynthesizer(router)(state(claims=claims, conflicts=[conflict]))  # type: ignore[arg-type]

    business = next(p for p in router.prompts if "SECTION: Business Model" in p)
    assert "never pick a winner" in business
    assert "MAJOR: 40% versus 49%." in business


# ---------------------------------------------------------------------------
# Citation validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fabricated_marker_is_stripped_from_the_rendered_report() -> None:
    router = FakeRouter(lambda _: prose("Revenue rose [1]. Something else [9]."))
    report = await ReportSynthesizer(router)(state())  # type: ignore[arg-type]

    assert "[9]" not in report
    assert "[1]" in report


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_failing_section_still_renders_with_its_band_and_sources() -> None:
    def handler(prompt: str) -> str | BaseException:
        if "SECTION: Competitive" in prompt:
            return RuntimeError("model said no")
        return prose()

    report = await ReportSynthesizer(FakeRouter(handler))(state())  # type: ignore[arg-type]
    signature = structural_signature(report)

    assert len(signature.sections) == 3
    assert "Synthesis for this section did not complete" in report
    assert signature.sections[1].has_badge
    assert signature.sections[1].has_sources_table


@pytest.mark.asyncio
async def test_unusable_json_degrades_the_section_rather_than_the_report() -> None:
    def handler(prompt: str) -> str:
        return "not json at all" if "SECTION: Company Quality" in prompt else prose()

    report = await ReportSynthesizer(FakeRouter(handler))(state())  # type: ignore[arg-type]

    assert len(structural_signature(report).sections) == 3
    assert "Synthesis for this section did not complete" in report


@pytest.mark.asyncio
async def test_total_provider_failure_propagates_rather_than_thinning_the_report() -> None:
    """The graph turns this into a PROVIDER_FAILURE halt. Rendering it as a
    sparse report would make an outage indistinguishable from an honest report
    about a thinly documented company — the one failure this project refuses."""
    router = FakeRouter(lambda _: AllProvidersFailedError("everything is down"))

    with pytest.raises(AllProvidersFailedError):
        await ReportSynthesizer(router)(state())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_gather_failures_are_disclosed_in_the_report() -> None:
    failure = GatherFailure(
        source="officers", kind=FailureKind.UNAVAILABLE, detail="Companies House returned 503"
    )
    report = await ReportSynthesizer(FakeRouter(lambda _: prose()))(  # type: ignore[arg-type]
        state(failures=[failure])
    )

    # A register failure bears on all three sections, since register documents
    # serve all three.
    assert report.count("officers could not be retrieved") == 3


# ---------------------------------------------------------------------------
# Phase 5.7 — cross-provider structural equivalence, under fakes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_and_fallback_prose_produce_identical_shape() -> None:
    """The unit-level half of 5.7. The live half is in `BUILD_LOG.md` Phase 5.

    Both routers are given the same evidence and return wildly different prose.
    The shapes must be equal, not similar — a fallback that produces a
    differently-shaped report is a real failure mode, not a cosmetic one.
    """
    claude = FakeRouter(lambda _: prose("Monzo is a UK bank [1], active on the register [1]."))
    gemini = FakeRouter(
        lambda _: prose(
            "The company, incorporated in 2015, operates as a deposit-taking bank [1]. "
            "Its register entry is current [1]."
        ),
        used_fallback=True,
        provider="gemini",
    )

    left = await ReportSynthesizer(claude)(state())  # type: ignore[arg-type]
    right = await ReportSynthesizer(gemini)(state())  # type: ignore[arg-type]

    assert structural_signature(left).differences(structural_signature(right)) == []


def test_the_resolution_line_states_how_the_company_was_identified() -> None:
    """Disambiguation is graded; a report that does not say which of several
    possible companies it is about is not traceable."""
    profile = CompanyProfile(company_number="09446231", company_name="MONZO BANK LIMITED")
    resolved = Resolution(kind=ResolutionKind.RESOLVED, query="Monzo", profile=profile)
    flagged = Resolution(
        kind=ResolutionKind.NEEDS_CONFIRMATION,
        query="Mondo",
        profile=profile,
        reasons=[ConfirmationReason.PREVIOUS_NAME],
    )

    assert "Exact match" in resolution_line(resolved)
    # The reasons themselves are named, not summarised away: "matched on a former
    # name" and "the company is dissolved" are different things for a reader to
    # act on, and Phase 1 made them a list precisely so both survive.
    assert "previous_name" in resolution_line(flagged)
    assert resolution_line(None) == "Not recorded."
