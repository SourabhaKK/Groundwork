"""Tests for claim extraction and conflict detection (Phases 4.2 and 4.3).

Three properties carry most of the weight here, and each exists because of a
specific failure this project has already met:

1. **A document carrying an `entity_warning` cannot produce a scoring claim.**
   TASKS.md 4.2 makes this a hard requirement after the Phase 2 live trial
   returned a Better Business Bureau page for "Monzo, Inc." in answer to a
   question about MONZO BANK LIMITED. It is tested with an unwarned twin as the
   control, and end-to-end through `score_confidence` so the band is shown to
   move — a test that only asserted a flag would not prove the mechanism does
   anything.
2. **Citations are structural.** The model has no field through which a URL can
   arrive, so a fabricated citation is unrepresentable rather than discouraged.
3. **Identity is per section** (the Phase 4 fix to `EvidenceClaim.id`): a URL
   gathered for two sections must survive as two claims, while the same document
   re-extracted on Phase 3.5's second pass must still collapse to one.

No test here calls a live provider. The live pass that read real extracted
claims is recorded in `BUILD_LOG.md` Phase 4.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from src.agent.claims import ConflictSeverity, EvidenceClaim, merge_claims
from src.agent.extract import (
    MAX_CLAIMS_PER_DOCUMENT,
    MAX_DOCUMENT_CHARS,
    ClaimExtractor,
    build_prompt,
    quote_is_grounded,
)
from src.data_sources.sources import EvidenceBundle, SourceType, TaggedDocument
from src.data_sources.web_gather import WebSection
from src.llm.router import AllProvidersFailedError, LLMResponse

COMPANY = "MONZO BANK LIMITED"
NUMBER = "09446231"

BUSINESS = WebSection.BUSINESS_MODEL
COMPETITIVE = WebSection.COMPETITIVE_LANDSCAPE
REPUTATION = WebSection.REPUTATION


# ---------------------------------------------------------------------------
# Doubles and builders
# ---------------------------------------------------------------------------


class FakeRouter:
    """Stands in for `LLMRouter`, scripted by a handler over the prompt.

    Scripted on the prompt rather than on call order because extraction runs
    documents concurrently — an order-keyed fake would make these tests depend
    on scheduling, which is exactly the kind of flakiness that gets a suite
    ignored.
    """

    def __init__(self, handler: Callable[[str], str | BaseException]) -> None:
        self._handler = handler
        self.prompts: list[str] = []

    async def complete(
        self,
        prompt: str,
        *,
        tier: object,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        self.prompts.append(prompt)
        result = self._handler(prompt)
        if isinstance(result, BaseException):
            raise result
        return LLMResponse(text=result, provider="fake", model="fake-model", used_fallback=False)


def claims_payload(*claims: dict) -> str:
    return json.dumps({"claims": list(claims)})


def conflicts_payload(*conflicts: dict) -> str:
    return json.dumps({"conflicts": list(conflicts)})


NO_CLAIMS = claims_payload()
NO_CONFLICTS = conflicts_payload()


def document(
    *,
    url: str = "https://www.fca.org.uk/news/example",
    content: str = "The FCA imposed a financial penalty of £21,091,300 on Monzo Bank Ltd.",
    source_type: SourceType = SourceType.OFFICIAL_REGISTRY,
    section: WebSection | None = BUSINESS,
    entity_warning: str | None = None,
    title: str | None = "Example document",
) -> TaggedDocument:
    return TaggedDocument(
        url=url,
        source_type=source_type,
        retrieved_at=datetime(2026, 8, 1, tzinfo=UTC),
        content=content,
        title=title,
        section=section,
        origin="test",
        entity_warning=entity_warning,
    )


def bundle(*documents: TaggedDocument) -> EvidenceBundle:
    return EvidenceBundle(company_number=NUMBER, company_name=COMPANY, documents=list(documents))


def responder(claims: str = NO_CLAIMS, conflicts: str = NO_CONFLICTS) -> Callable[[str], str]:
    """Route by prompt shape: the conflict prompt is the one asking for conflicts."""

    def handle(prompt: str) -> str:
        return conflicts if "genuinely disagree" in prompt else claims

    return handle


def extractor(handler: Callable[[str], str | BaseException]) -> ClaimExtractor:
    return ClaimExtractor(FakeRouter(handler))  # type: ignore[arg-type]


ALL = tuple(WebSection)


# ---------------------------------------------------------------------------
# Quote verification — the mechanical half of the grounding check
# ---------------------------------------------------------------------------


def test_a_verbatim_quote_is_grounded() -> None:
    content = "Monzo reported revenue of £1.7bn in the year to February 2026."
    assert quote_is_grounded("revenue of £1.7bn in the year", content)


def test_typographic_differences_do_not_fail_a_real_quote() -> None:
    """Models reproduce a sentence while normalising its typography.

    Failing a claim over a curly apostrophe would make this check measure
    transcription fidelity rather than grounding, and would quietly strip real
    evidence out of the scoring math.
    """
    content = "The company’s revenue — some £1.7bn — grew\nsharply last year."
    assert quote_is_grounded("The company's revenue - some £1.7bn - grew sharply", content)


def test_a_fabricated_quote_is_not_grounded() -> None:
    content = "The FCA imposed a financial penalty of £21,091,300 on Monzo Bank Ltd."
    assert not quote_is_grounded("Monzo was found to have committed fraud", content)


def test_a_trivially_short_quote_is_not_grounded() -> None:
    """ "Monzo" appears in every document about Monzo, so accepting a short span
    would make the check pass on any claim at all."""
    assert not quote_is_grounded("Monzo", "Monzo Bank Limited is a UK bank.")


# ---------------------------------------------------------------------------
# Provenance: the model cannot supply a citation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_provenance_comes_from_the_document_not_the_model() -> None:
    doc = document(
        url="https://www.fca.org.uk/news/real",
        content="The FCA imposed a financial penalty of £21,091,300 on Monzo Bank Ltd.",
        source_type=SourceType.OFFICIAL_REGISTRY,
        section=REPUTATION,
    )
    # The response carries a plausible-looking but invented citation. There is no
    # field for it, so it cannot reach the claim.
    payload = claims_payload(
        {
            "text": "The FCA fined Monzo Bank Ltd £21,091,300.",
            "quote": "financial penalty of £21,091,300",
            "confidence": "direct",
            "source_url": "https://www.fca.org.uk/invented-page",
            "source_type": "trade_press",
        }
    )

    result = await extractor(responder(payload))(bundle(doc), ALL)

    assert len(result.claims) == 1
    claim = result.claims[0]
    assert claim.source_url == "https://www.fca.org.uk/news/real"
    assert claim.source_type is SourceType.OFFICIAL_REGISTRY
    assert claim.section is REPUTATION
    assert claim.origin == "test"
    assert claim.counts_toward_confidence


@pytest.mark.asyncio
async def test_an_ungrounded_claim_is_kept_but_not_scored() -> None:
    """Phase 4.1's rule: excluded claims still render in prose. Deleting them
    would hide the very thinness the band is reporting."""
    doc = document(content="Monzo Bank Limited was incorporated on 18 February 2015.")
    payload = claims_payload(
        {"text": "Monzo has 15.2m customers.", "quote": "Monzo has 15.2 million customers"}
    )

    result = await extractor(responder(payload))(bundle(doc), ALL)

    assert len(result.claims) == 1
    claim = result.claims[0]
    assert not claim.counts_toward_confidence
    assert "verbatim" in (claim.exclusion_reason or "")


# ---------------------------------------------------------------------------
# The entity-warning requirement (TASKS.md 4.2, non-optional)
# ---------------------------------------------------------------------------

WARNED_CONTENT = (
    "Monzo, Inc. Better Business Bureau complaints. Customers have filed 42 "
    "complaints against Monzo, Inc. in the last three years."
)
WARNED_PAYLOAD = claims_payload(
    {
        "text": "Customers filed 42 complaints in the last three years.",
        "quote": "filed 42 complaints against Monzo, Inc. in the last three years",
        "confidence": "direct",
    }
)


@pytest.mark.asyncio
async def test_a_warned_document_cannot_produce_a_scoring_claim() -> None:
    doc = document(
        url="https://www.bbb.org/us/monzo-inc",
        content=WARNED_CONTENT,
        source_type=SourceType.REVIEW,
        section=REPUTATION,
        entity_warning="Monzo, Inc.",
    )

    result = await extractor(responder(WARNED_PAYLOAD))(bundle(doc), ALL)

    claim = result.claims[0]
    assert not claim.counts_toward_confidence
    assert claim.entity_warning == "Monzo, Inc."
    assert "Monzo, Inc." in (claim.exclusion_reason or "")
    # The mismatch is stated in the claim's own text, so it cannot be read as a
    # fact about the UK company even if the prose is rendered without the flag.
    assert claim.text.startswith('[Refers to "Monzo, Inc.", not MONZO BANK LIMITED]')
    assert "42 complaints" in claim.text


@pytest.mark.asyncio
async def test_the_control_an_identical_unwarned_document_does_score() -> None:
    """Without this, the test above would pass on a bug that excluded everything."""
    doc = document(
        url="https://www.bbb.org/us/monzo-inc",
        content=WARNED_CONTENT,
        source_type=SourceType.REVIEW,
        section=REPUTATION,
        entity_warning=None,
    )

    result = await extractor(responder(WARNED_PAYLOAD))(bundle(doc), ALL)

    claim = result.claims[0]
    assert claim.counts_toward_confidence
    assert not claim.text.startswith("[Refers to")


@pytest.mark.asyncio
async def test_the_warning_is_enforced_even_if_the_model_ignores_the_prompt() -> None:
    """The prompt asks; the code enforces. A prompt instruction is a request, and
    "must not silently absorb" is only true if the claim *cannot* score."""
    doc = document(
        content=WARNED_CONTENT,
        source_type=SourceType.REVIEW,
        section=REPUTATION,
        entity_warning="Monzo, Inc.",
    )
    # A maximally uncooperative response: the fact attributed flatly to the UK
    # company, with nothing to indicate a different entity.
    payload = claims_payload(
        {
            "text": "Monzo Bank Limited has 42 unresolved customer complaints.",
            "quote": "filed 42 complaints against Monzo, Inc. in the last three years",
        }
    )

    result = await extractor(responder(payload))(bundle(doc), ALL)

    assert not result.claims[0].counts_toward_confidence
    assert result.claims[0].text.startswith('[Refers to "Monzo, Inc."')


@pytest.mark.asyncio
async def test_the_entity_prefix_is_not_applied_twice() -> None:
    doc = document(
        content=WARNED_CONTENT,
        source_type=SourceType.REVIEW,
        section=REPUTATION,
        entity_warning="Monzo, Inc.",
    )
    payload = claims_payload(
        {
            "text": (
                '[Refers to "Monzo, Inc.", not MONZO BANK LIMITED] Customers filed 42 complaints.'
            ),
            "quote": "filed 42 complaints against Monzo, Inc. in the last three years",
        }
    )

    result = await extractor(responder(payload))(bundle(doc), ALL)

    assert result.claims[0].text.count("[Refers to") == 1


@pytest.mark.asyncio
async def test_a_warned_document_cannot_lift_a_sections_confidence_band() -> None:
    """End-to-end through the rule the report publishes.

    Asserting the flag alone would not show the mechanism does anything. Three
    warned claims would otherwise satisfy HIGH's count-and-authority test.
    """
    from src.report.confidence import ConfidenceBand, ConfidenceReason, score_confidence

    content = (
        "Monzo, Inc. holds a US banking licence. Monzo, Inc. reported 42 complaints. "
        "Monzo, Inc. operates from New York."
    )
    payload = claims_payload(
        {
            "text": "It holds a US banking licence.",
            "quote": "Monzo, Inc. holds a US banking licence",
        },
        {"text": "It reported 42 complaints.", "quote": "Monzo, Inc. reported 42 complaints"},
        {"text": "It operates from New York.", "quote": "Monzo, Inc. operates from New York"},
    )
    doc = document(
        content=content,
        source_type=SourceType.OFFICIAL_REGISTRY,
        section=REPUTATION,
        entity_warning="Monzo, Inc.",
    )

    result = await extractor(responder(payload))(bundle(doc), ALL)
    assessment = score_confidence(result.claims)

    assert len(result.claims) == 3  # all three still render in prose
    assert assessment.band is ConfidenceBand.LOW
    assert assessment.reason is ConfidenceReason.NO_CLAIMS
    assert assessment.total_claims == 3
    assert assessment.scored_claims == 0


def test_the_warned_prompt_states_the_mismatch() -> None:
    doc = document(
        content=WARNED_CONTENT,
        source_type=SourceType.REVIEW,
        section=REPUTATION,
        entity_warning="Monzo, Inc.",
    )
    prompt = build_prompt(doc, bundle(doc))

    assert "ENTITY MISMATCH" in prompt
    assert "Monzo, Inc." in prompt
    # It asks for the facts to be extracted and attributed elsewhere, not
    # withheld. The live pass found the first wording produced a flat refusal,
    # which silently reverted to the option this design rejected: dropping the
    # evidence instead of surfacing it under the right entity.
    assert "Still extract the facts this document supports" in prompt
    assert "must not do is\nattribute any of them to MONZO BANK LIMITED" in prompt


# ---------------------------------------------------------------------------
# Prompts: four variants, one granularity rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("source_type", "marker"),
    [
        (SourceType.OFFICIAL_REGISTRY, "independent public body"),
        (SourceType.OFFICIAL_SELF, "self-interested"),
        (SourceType.TRADE_PRESS, "third-party writing"),
        (SourceType.GENERAL_WEB, "third-party writing"),
        (SourceType.REVIEW, "review platform"),
    ],
)
def test_each_source_type_gets_its_own_framing(source_type: SourceType, marker: str) -> None:
    """Four framings across five tiers: `general_web` and `trade_press` differ in
    authority weight, which 5.4 applies at scoring, not in what counts as a fact."""
    doc = document(source_type=source_type)
    assert marker in build_prompt(doc, bundle(doc))


@pytest.mark.parametrize("source_type", list(SourceType))
def test_every_prompt_carries_the_granularity_tie_breaker(source_type: SourceType) -> None:
    doc = document(source_type=source_type)
    prompt = build_prompt(doc, bundle(doc))
    assert "ONE atomic, independently-citable fact" in prompt
    assert "only make sense as a qualifier to another fact" in prompt


def test_a_web_document_inherits_its_section_and_is_not_asked_to_choose() -> None:
    doc = document(section=COMPETITIVE)
    prompt = build_prompt(doc, bundle(doc))
    assert 'gathered as evidence for the "competitive landscape" section' in prompt
    assert '"section"    - one of' not in prompt


def test_a_register_document_is_asked_to_route_each_claim_to_a_section() -> None:
    """A register document serves all three sections. Extracting it once per
    section would let one incorporation date pad every section's count."""
    doc = document(section=None)
    prompt = build_prompt(doc, bundle(doc))
    assert "each claim must say which section it" in prompt
    assert '"business_model"' in prompt


def test_long_documents_are_truncated_for_the_prompt_only() -> None:
    tail = "The company was incorporated on 18 February 2015 in England and Wales."
    doc = document(content=("filler. " * 2000) + tail)
    assert len(doc.content) > MAX_DOCUMENT_CHARS

    prompt = build_prompt(doc, bundle(doc))
    assert "[...document truncated]" in prompt
    # Verification still runs against the *full* content, so truncation can never
    # turn a genuine quote into a rejected one.
    assert quote_is_grounded(tail, doc.content)


# ---------------------------------------------------------------------------
# Which documents a pass extracts from
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_full_pass_extracts_web_and_register_documents() -> None:
    docs = [
        document(url="https://example.com/a", section=BUSINESS),
        document(url="https://example.com/b", section=REPUTATION),
        document(url="https://find-and-update.company-information.service.gov.uk/x", section=None),
    ]
    router = FakeRouter(responder())
    await ClaimExtractor(router)(bundle(*docs), ALL)  # type: ignore[arg-type]

    assert len(router.prompts) == 3


@pytest.mark.asyncio
async def test_a_second_pass_extracts_only_the_re_searched_section() -> None:
    """Phase 3.5 narrows `sections` to what it re-searched. The register has
    already been extracted, and re-deriving its claims would spend tokens on
    duplicates `merge_claims` then discards."""
    docs = [
        document(url="https://example.com/a", section=BUSINESS),
        document(url="https://example.com/b", section=REPUTATION),
        document(url="https://find-and-update.company-information.service.gov.uk/x", section=None),
    ]
    router = FakeRouter(responder())
    await ClaimExtractor(router)(bundle(*docs), [REPUTATION])  # type: ignore[arg-type]

    assert len(router.prompts) == 1
    assert "https://example.com/b" in router.prompts[0]


@pytest.mark.asyncio
async def test_an_empty_document_costs_no_model_call() -> None:
    router = FakeRouter(responder())
    result = await ClaimExtractor(router)(bundle(document(content="   ")), ALL)  # type: ignore[arg-type]

    assert router.prompts == []
    assert result.claims == []


# ---------------------------------------------------------------------------
# Register documents route their own claims
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_register_claim_is_filed_under_the_section_the_model_chose() -> None:
    content = (
        "MONZO BANK LIMITED (company number 09446231)\nincorporated: 2015-02-18\n"
        "SIC codes: 64191, 64999"
    )
    payload = claims_payload(
        {
            "text": "Monzo Bank Limited was incorporated on 18 February 2015.",
            "quote": "incorporated: 2015-02-18",
            "section": "business_model",
        },
        {
            "text": "Monzo Bank Limited is registered under SIC codes 64191 and 64999.",
            "quote": "SIC codes: 64191, 64999",
            "section": "reputation",
        },
    )
    result = await extractor(responder(payload))(
        bundle(document(section=None, content=content)), ALL
    )

    assert [claim.section for claim in result.claims] == [BUSINESS, REPUTATION]


@pytest.mark.asyncio
async def test_a_register_claim_naming_no_section_is_dropped() -> None:
    """It has nowhere to be scored or rendered, and guessing would file a fact
    under a heading the model never chose."""
    payload = claims_payload({"text": "Something true.", "quote": "incorporated: 2015-02-18"})
    doc = document(section=None, content="incorporated: 2015-02-18")

    result = await extractor(responder(payload))(bundle(doc), ALL)

    assert result.claims == []


# ---------------------------------------------------------------------------
# The over-fragmentation guard
# ---------------------------------------------------------------------------


def _many(count: int) -> tuple[str, str]:
    content = " ".join(f"Fact number {i} is recorded in this document." for i in range(count))
    payload = claims_payload(
        *(
            {
                "text": f"Fact {i}.",
                "quote": f"Fact number {i} is recorded in this document",
            }
            for i in range(count)
        )
    )
    return content, payload


@pytest.mark.asyncio
async def test_over_fragmentation_costs_scoring_but_never_deletes_evidence() -> None:
    count = MAX_CLAIMS_PER_DOCUMENT + 1
    content, payload = _many(count)
    result = await extractor(responder(payload))(bundle(document(content=content)), ALL)

    assert len(result.claims) == count  # nothing deleted — all still render in prose
    assert not any(claim.counts_toward_confidence for claim in result.claims)
    assert "over-fragmentation" in (result.claims[0].exclusion_reason or "")


@pytest.mark.asyncio
async def test_a_document_at_the_guard_still_scores() -> None:
    content, payload = _many(MAX_CLAIMS_PER_DOCUMENT)
    result = await extractor(responder(payload))(bundle(document(content=content)), ALL)

    assert all(claim.counts_toward_confidence for claim in result.claims)


# ---------------------------------------------------------------------------
# Failure behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_documents_failure_does_not_cost_the_others() -> None:
    good = document(url="https://example.com/good", content="Monzo was incorporated in 2015.")
    bad = document(url="https://example.com/bad", content="Some other content entirely.")
    payload = claims_payload(
        {"text": "Monzo was incorporated in 2015.", "quote": "Monzo was incorporated in 2015"}
    )

    def handle(prompt: str) -> str | BaseException:
        if "https://example.com/bad" in prompt:
            return RuntimeError("provider hiccup")
        return responder(payload)(prompt)

    result = await extractor(handle)(bundle(good, bad), ALL)

    assert len(result.claims) == 1
    assert result.claims[0].source_url == "https://example.com/good"


@pytest.mark.asyncio
async def test_a_total_provider_outage_propagates_rather_than_degrading() -> None:
    """An outage rendered as a thin report is indistinguishable from an honest
    report about a thinly-documented company. `extract_node` turns this into a
    PROVIDER_FAILURE halt."""

    def handle(prompt: str) -> BaseException:
        return AllProvidersFailedError("primary and fallback both down")

    with pytest.raises(AllProvidersFailedError):
        await extractor(handle)(bundle(document()), ALL)


@pytest.mark.asyncio
async def test_an_unusable_response_yields_no_claims_and_does_not_raise() -> None:
    result = await extractor(responder("I'm afraid I can't help with that."))(
        bundle(document()), ALL
    )
    assert result.claims == []


@pytest.mark.asyncio
async def test_json_wrapped_in_a_code_fence_is_still_read() -> None:
    payload = claims_payload(
        {"text": "Monzo was incorporated in 2015.", "quote": "Monzo was incorporated in 2015"}
    )
    fenced = f"Here you go:\n```json\n{payload}\n```"
    doc = document(content="Monzo was incorporated in 2015.")

    result = await extractor(responder(fenced))(bundle(doc), ALL)

    assert len(result.claims) == 1


# ---------------------------------------------------------------------------
# Phase 4.3 — conflict detection
# ---------------------------------------------------------------------------

TWO_CLAIM_CONTENT = (
    "Monzo employed around 3,000 people in 2025. Monzo employed about 2,800 staff in 2025."
)
TWO_CLAIMS = claims_payload(
    {
        "text": "Monzo employed around 3,000 people in 2025.",
        "quote": "employed around 3,000 people",
    },
    {"text": "Monzo employed about 2,800 staff in 2025.", "quote": "employed about 2,800 staff"},
)


@pytest.mark.asyncio
async def test_a_conflict_references_real_claim_ids() -> None:
    conflicts = conflicts_payload(
        {"claims": [1, 2], "severity": "minor", "detail": "employee counts differ"}
    )
    doc = document(content=TWO_CLAIM_CONTENT, section=BUSINESS)

    result = await extractor(responder(TWO_CLAIMS, conflicts))(bundle(doc), ALL)

    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert conflict.severity is ConflictSeverity.MINOR
    assert conflict.section is BUSINESS
    assert set(conflict.claim_ids) == {claim.id for claim in result.claims}


@pytest.mark.asyncio
async def test_a_conflict_citing_a_claim_that_does_not_exist_is_dropped() -> None:
    """Inventing which claim was meant would attach a disagreement to evidence
    that never carried it."""
    conflicts = conflicts_payload(
        {"claims": [1, 9], "severity": "major", "detail": "hallucinated pairing"}
    )
    doc = document(content=TWO_CLAIM_CONTENT, section=BUSINESS)

    result = await extractor(responder(TWO_CLAIMS, conflicts))(bundle(doc), ALL)

    assert result.conflicts == []


@pytest.mark.asyncio
async def test_a_conflict_citing_one_claim_twice_is_dropped() -> None:
    conflicts = conflicts_payload(
        {"claims": [2, 2], "severity": "minor", "detail": "self-conflict"}
    )
    doc = document(content=TWO_CLAIM_CONTENT, section=BUSINESS)

    result = await extractor(responder(TWO_CLAIMS, conflicts))(bundle(doc), ALL)

    assert result.conflicts == []


@pytest.mark.asyncio
async def test_a_major_conflict_reaches_the_confidence_rule() -> None:
    from src.report.confidence import ConfidenceBand, ConfidenceReason, score_confidence

    conflicts = conflicts_payload(
        {
            "claims": [1, 2],
            "severity": "major",
            "detail": "one says dissolved, the register says active",
        }
    )
    doc = document(content=TWO_CLAIM_CONTENT, section=BUSINESS)

    result = await extractor(responder(TWO_CLAIMS, conflicts))(bundle(doc), ALL)
    assessment = score_confidence(result.claims, result.conflicts)

    assert assessment.band is ConfidenceBand.LOW
    assert assessment.reason is ConfidenceReason.MAJOR_CONFLICT


@pytest.mark.asyncio
async def test_a_section_with_one_claim_costs_no_conflict_call() -> None:
    payload = claims_payload(
        {"text": "Monzo was incorporated in 2015.", "quote": "Monzo was incorporated in 2015"}
    )
    router = FakeRouter(responder(payload))
    await ClaimExtractor(router)(  # type: ignore[arg-type]
        bundle(document(content="Monzo was incorporated in 2015.", section=BUSINESS)), ALL
    )

    assert not any("genuinely disagree" in prompt for prompt in router.prompts)


@pytest.mark.asyncio
async def test_a_failed_conflict_pass_degrades_without_losing_claims() -> None:
    """A missed conflict leaves a section scored as if its evidence agreed — the
    optimistic direction — but failing a whole report over a comparison pass is
    worse, and the claims themselves are untouched."""

    def handle(prompt: str) -> str | BaseException:
        if "genuinely disagree" in prompt:
            return RuntimeError("comparison failed")
        return TWO_CLAIMS

    result = await extractor(handle)(bundle(document(content=TWO_CLAIM_CONTENT)), ALL)

    assert len(result.claims) == 2
    assert result.conflicts == []


@pytest.mark.asyncio
async def test_the_conflict_pass_never_resolves_anything() -> None:
    """Both sides of a disagreement survive extraction. Phase 4.3's requirement
    is that conflicts surface, not that one side wins."""
    conflicts = conflicts_payload(
        {"claims": [1, 2], "severity": "minor", "detail": "employee counts differ"}
    )
    result = await extractor(responder(TWO_CLAIMS, conflicts))(
        bundle(document(content=TWO_CLAIM_CONTENT)), ALL
    )

    assert len(result.claims) == 2
    assert all(claim.counts_toward_confidence for claim in result.claims)


# ---------------------------------------------------------------------------
# Claim identity (the Phase 4 fix to EvidenceClaim.id)
# ---------------------------------------------------------------------------


def _claim(section: WebSection, text: str = "Monzo is a UK bank.") -> EvidenceClaim:
    return EvidenceClaim(
        text=text,
        source_url="https://en.wikipedia.org/wiki/Monzo",
        source_type=SourceType.GENERAL_WEB,
        section=section,
    )


def test_the_same_url_and_text_in_two_sections_are_two_claims() -> None:
    """Phase 2's live trial saw Wikipedia returned in two sections. Hashing only
    url+text collapsed them into one, silently costing the second section a claim
    it had genuinely gathered — and Phase 5.4 counts per section."""
    assert _claim(BUSINESS).id != _claim(COMPETITIVE).id


def test_merge_keeps_both_sections_copies() -> None:
    merged = merge_claims([_claim(BUSINESS)], [_claim(COMPETITIVE)])
    assert len(merged) == 2
    assert {claim.section for claim in merged} == {BUSINESS, COMPETITIVE}


def test_re_extracting_the_same_document_still_collapses() -> None:
    """Second-pass idempotence, the property the id was content-addressed for:
    a re-search that returns a document already seen must not double its claims."""
    merged = merge_claims([_claim(REPUTATION)], [_claim(REPUTATION)])
    assert len(merged) == 1


@pytest.mark.asyncio
async def test_extraction_is_idempotent_across_two_passes() -> None:
    """The end-to-end version: the same document extracted twice yields one claim
    after merge, so a re-search cannot inflate a band with evidence it already had."""
    doc = document(content="Monzo was incorporated in 2015.", section=REPUTATION)
    payload = claims_payload(
        {"text": "Monzo was incorporated in 2015.", "quote": "Monzo was incorporated in 2015"}
    )
    extract = extractor(responder(payload))

    first = await extract(bundle(doc), ALL)
    second = await extract(bundle(doc), [REPUTATION])

    assert len(merge_claims(first.claims, second.claims)) == 1
