"""Phase 3.5's query refinement, and the guardrails around it.

The thing worth testing here is not that an LLM can write a query — it is that a
*bad* refinement never reaches Tavily. The failure mode this module exists to
prevent is a model returning the original query with different adjectives, which
would spend a credit re-retrieving the same documents and produce a re-search
that flatters itself.
"""

from __future__ import annotations

import pytest

from src.agent.research import (
    MAX_QUERY_LENGTH,
    QueryRefinement,
    RefinementSource,
    fallback_query,
    refine_query,
    similarity,
    validate_refinement,
)
from src.data_sources.companies_house import CompanyProfile
from src.data_sources.web_gather import WebSection, build_queries
from src.llm.router import LLMResponse

SECTION = WebSection.REPUTATION


@pytest.fixture
def profile() -> CompanyProfile:
    return CompanyProfile(number="09446231", name="MONZO BANK LIMITED", status="active")


class FakeRouter:
    """Stands in for `LLMRouter`. Records the prompt so the test can assert the
    model was actually told what the gap was."""

    def __init__(self, reply: str | Exception) -> None:
        self._reply = reply
        self.prompts: list[str] = []

    async def complete(self, prompt: str, **_: object) -> LLMResponse:
        self.prompts.append(prompt)
        if isinstance(self._reply, Exception):
            raise self._reply
        return LLMResponse(
            text=self._reply, provider="anthropic", model="claude-haiku-4-5", used_fallback=False
        )


# ---------------------------------------------------------------------------
# Validation — the part that protects the credit
# ---------------------------------------------------------------------------


def test_a_paraphrase_of_the_original_is_rejected() -> None:
    original = "Monzo UK customer reviews, complaints, reputation and regulatory action"
    paraphrase = "Monzo UK customer reviews, complaints and reputation plus regulatory action"

    query, reason = validate_refinement(paraphrase, original=original, short="Monzo")

    assert query is None
    assert "too similar" in (reason or "")


def test_a_genuinely_different_angle_is_accepted() -> None:
    original = "Monzo UK customer reviews, complaints, reputation and regulatory action"
    refined = "Monzo UK Financial Ombudsman decisions upheld against the bank in 2025"

    query, reason = validate_refinement(refined, original=original, short="Monzo")

    assert reason is None
    assert query == refined


def test_a_query_that_drops_the_company_name_is_rejected() -> None:
    """Results from a query about the sector would be attributed to this company
    in the report — a grounding failure, not a relevance one."""
    query, reason = validate_refinement(
        "UK challenger bank complaints and ombudsman decisions",
        original="Monzo UK customer reviews",
        short="Monzo",
    )

    assert query is None
    assert "does not name the company" in (reason or "")


def test_a_missing_uk_anchor_is_repaired_rather_than_rejected() -> None:
    """Unambiguous repair, so rejecting would waste a usable refinement. The
    anchor exists because a live trial returned a US affiliate's complaints page
    for a query about the UK bank."""
    query, reason = validate_refinement(
        "Monzo Financial Ombudsman decisions upheld against the bank",
        original="Monzo UK customer reviews and complaints",
        short="Monzo",
    )

    assert reason is None
    assert query is not None and query.endswith(" UK")


@pytest.mark.parametrize(
    "already_anchored",
    [
        "Monzo Financial Ombudsman decisions UK",
        "Monzo ombudsman decisions in the United Kingdom",
        "Monzo UK ombudsman decisions",
    ],
)
def test_an_existing_uk_anchor_is_not_duplicated(already_anchored: str) -> None:
    """Regression: the anchor check originally ran through `_tokens`, which
    drops anything under three characters and therefore could never contain
    "uk" — so the repair fired unconditionally and appended a second anchor."""
    query, reason = validate_refinement(
        already_anchored, original="Monzo customer reviews and complaints", short="Monzo"
    )

    assert reason is None
    assert query is not None
    assert query.lower().count("uk") <= 1
    assert not query.endswith("UK UK")


def test_an_overlong_query_is_rejected() -> None:
    query, reason = validate_refinement(
        "Monzo UK " + "ombudsman " * 40, original="Monzo UK reviews", short="Monzo"
    )

    assert query is None
    assert "too long" in (reason or "")


def test_an_empty_query_is_rejected() -> None:
    query, reason = validate_refinement("   ", original="Monzo UK reviews", short="Monzo")
    assert query is None
    assert reason == "empty query"


def test_similarity_is_symmetric_and_bounded() -> None:
    a, b = "Monzo UK customer complaints", "Monzo UK ombudsman decisions"
    assert similarity(a, b) == similarity(b, a)
    assert 0.0 <= similarity(a, b) <= 1.0
    assert similarity(a, a) == 1.0


# ---------------------------------------------------------------------------
# The fallback templates are a real floor, not decoration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("section", list(WebSection))
def test_every_fallback_template_would_itself_pass_validation(
    section: WebSection, profile: CompanyProfile
) -> None:
    """The fallback is what a rejected refinement degrades to. If a template
    were itself a paraphrase of the original, the guardrail would be circular —
    rejecting the model's paraphrase only to search with our own."""
    original = build_queries(profile)[section]
    fallback = fallback_query(section, profile)

    query, reason = validate_refinement(fallback, original=original, short="Monzo")

    assert reason is None, f"{section.value} fallback rejected: {reason}"
    assert query is not None
    assert len(query) <= MAX_QUERY_LENGTH


# ---------------------------------------------------------------------------
# refine_query — end to end, with a fake model
# ---------------------------------------------------------------------------


async def test_a_good_model_proposal_is_used(profile: CompanyProfile) -> None:
    router = FakeRouter(
        '{"query": "Monzo UK Financial Ombudsman upheld decisions and complaint outcomes",'
        ' "targets_gap": "adjudicated complaint outcomes",'
        ' "differs_because": "seeks regulator adjudications rather than review-site sentiment"}'
    )

    refinement = await refine_query(
        section=SECTION,
        profile=profile,
        gap="no usable claims at all for reputation",
        seen_urls=["https://trustpilot.com/review/monzo.com"],
        router=router,  # type: ignore[arg-type]
    )

    assert refinement.source is RefinementSource.MODEL
    assert "Ombudsman" in refinement.query
    assert refinement.query != refinement.original
    assert refinement.rejection_reason is None


async def test_the_model_is_told_the_gap_and_what_was_already_seen(
    profile: CompanyProfile,
) -> None:
    """The non-negotiable is that the refinement targets the *specific* gap. It
    cannot do that if the gap never reaches the prompt."""
    router = FakeRouter('{"query": "Monzo UK ombudsman decisions", "targets_gap": "x"}')

    await refine_query(
        section=SECTION,
        profile=profile,
        gap="only 1 scored claim of 4, and they are weak sources",
        seen_urls=["https://trustpilot.com/review/monzo.com"],
        router=router,  # type: ignore[arg-type]
    )

    prompt = router.prompts[0]
    assert "only 1 scored claim of 4" in prompt
    assert "trustpilot.com" in prompt
    assert "MONZO BANK LIMITED" in prompt


async def test_a_paraphrasing_model_falls_back_to_the_template(
    profile: CompanyProfile,
) -> None:
    original = build_queries(profile)[SECTION]
    router = FakeRouter(f'{{"query": "{original}", "targets_gap": "same thing again"}}')

    refinement = await refine_query(
        section=SECTION,
        profile=profile,
        gap="thin",
        seen_urls=[],
        router=router,  # type: ignore[arg-type]
    )

    assert refinement.source is RefinementSource.FALLBACK_TEMPLATE
    assert refinement.query == fallback_query(SECTION, profile)
    assert refinement.rejected_query == original
    assert "too similar" in (refinement.rejection_reason or "")


async def test_an_unparseable_reply_falls_back(profile: CompanyProfile) -> None:
    router = FakeRouter("Sure! Here's a great query for you.")

    refinement = await refine_query(
        section=SECTION,
        profile=profile,
        gap="thin",
        seen_urls=[],
        router=router,  # type: ignore[arg-type]
    )

    assert refinement.source is RefinementSource.FALLBACK_TEMPLATE
    assert refinement.rejection_reason == "model returned no usable JSON"


async def test_a_fenced_json_reply_is_still_parsed(profile: CompanyProfile) -> None:
    """Models fence JSON constantly. Falling back on that would make the
    adaptive step depend on formatting rather than on evidence."""
    router = FakeRouter(
        '```json\n{"query": "Monzo UK ombudsman upheld complaint decisions 2025",'
        ' "targets_gap": "adjudications"}\n```'
    )

    refinement = await refine_query(
        section=SECTION,
        profile=profile,
        gap="thin",
        seen_urls=[],
        router=router,  # type: ignore[arg-type]
    )

    assert refinement.source is RefinementSource.MODEL


async def test_a_provider_outage_degrades_to_the_template_and_never_raises(
    profile: CompanyProfile,
) -> None:
    """A re-search that vanished when the model misbehaved would make the
    adaptive step silently conditional on the LLM rather than on the evidence."""
    router = FakeRouter(RuntimeError("all providers failed"))

    refinement = await refine_query(
        section=SECTION,
        profile=profile,
        gap="thin",
        seen_urls=[],
        router=router,  # type: ignore[arg-type]
    )

    assert refinement.source is RefinementSource.FALLBACK_TEMPLATE
    assert "llm unavailable" in (refinement.rejection_reason or "")


def test_describe_names_the_source(profile: CompanyProfile) -> None:
    refinement = QueryRefinement(
        section=SECTION,
        original="a",
        query="b",
        source=RefinementSource.FALLBACK_TEMPLATE,
        rejection_reason="too similar",
    )

    assert "fallback_template" in refinement.describe()
    assert "too similar" in refinement.describe()
