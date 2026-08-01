"""The orchestration graph (Phase 3.1-3.5).

The tests that matter here are about *control flow*, not about any node's
output: whether the graph re-searches, and whether it declines to. A pipeline
with a retry bolted on would pass a test that only checked "a re-search
happened". These check that the branch is taken **because of what the evidence
turned out to be** and not taken when it shouldn't be — which is the difference
between an agent and a fixed sequence, and the specific property TASKS.md 3.5
was added to provide.

Every external call is faked. Per AGENTS.md no test may reach a live API, and
these fakes additionally let a test state "this company has thin reputation
evidence" directly, rather than constructing documents and hoping extraction
produces the claim count the branch needs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from src.agent.claims import (
    Conflict,
    ConflictSeverity,
    EvidenceClaim,
    ExtractionResult,
)
from src.agent.deps import AgentDependencies
from src.agent.graph import run_report
from src.agent.nodes import EVALUATE, RESEARCH
from src.agent.resolve import Candidate, Resolution, ResolutionKind
from src.agent.state import MAX_RESEARCH_SECTIONS, RunOutcome
from src.data_sources.companies_house import CompanyProfile
from src.data_sources.registry_gather import RegistryGather
from src.data_sources.sources import SourceType
from src.data_sources.tavily import SearchDepth, SearchResponse, SearchResult
from src.data_sources.web_gather import (
    BASELINE_CREDITS_PER_REPORT,
    SECTION_DEPTH,
    SectionSearch,
    WebGather,
    WebSection,
)
from src.llm.router import LLMResponse

PROFILE = CompanyProfile(number="09446231", name="MONZO BANK LIMITED", status="active")

BUSINESS = WebSection.BUSINESS_MODEL
COMPETITIVE = WebSection.COMPETITIVE_LANDSCAPE
REPUTATION = WebSection.REPUTATION


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _result(url: str, title: str = "t") -> SearchResult:
    return SearchResult(
        url=url, title=title, content="snippet text", retrieved_at=datetime.now(UTC)
    )


class FakeCompaniesHouse:
    def __init__(self, resolution: Resolution | None = None) -> None:
        self.resolution = resolution or Resolution(
            kind=ResolutionKind.RESOLVED, query="monzo", profile=PROFILE
        )


class FakeTavily:
    """Records every query it is asked for, which is how the re-search tests
    assert the second query differed from the first."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.depths: list[SearchDepth] = []

    async def search(self, query: str, *, depth: SearchDepth = SearchDepth.BASIC, **_: object):
        self.queries.append(query)
        self.depths.append(depth)
        return SearchResponse(
            query=query,
            depth=depth,
            results=[_result(f"https://example.com/{len(self.queries)}")],
            credits_spent=depth.credits,
        )


class FakeRouter:
    def __init__(self, query: str = "Monzo UK ombudsman upheld complaint decisions") -> None:
        self.query = query
        self.calls = 0

    async def complete(self, prompt: str, **_: object) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            text=f'{{"query": "{self.query}", "targets_gap": "adjudicated complaints",'
            ' "differs_because": "regulator adjudications, not review sentiment"}',
            provider="anthropic",
            model="claude-haiku-4-5",
            used_fallback=False,
        )


class ScriptedExtractor:
    """Returns a fixed set of claims per section, per pass.

    `second_pass` is what the re-search is allowed to discover. Keeping the two
    passes separate is what lets a test assert that a section only reached HIGH
    *because* the re-search found something.
    """

    def __init__(
        self,
        first_pass: dict[WebSection, list[EvidenceClaim]] | None = None,
        second_pass: dict[WebSection, list[EvidenceClaim]] | None = None,
        conflicts: list[Conflict] | None = None,
    ) -> None:
        self.first_pass = first_pass or {}
        self.second_pass = second_pass or {}
        self.conflicts = conflicts or []
        self.calls: list[tuple[WebSection, ...]] = []

    async def __call__(self, evidence, sections) -> ExtractionResult:
        source = self.first_pass if not self.calls else self.second_pass
        self.calls.append(tuple(sections))
        claims = [c for section in sections for c in source.get(section, [])]
        return ExtractionResult(
            claims=claims, conflicts=self.conflicts if len(self.calls) == 1 else []
        )


async def _synthesize(state: object) -> str:
    return "REPORT"


def claim(
    section: WebSection,
    text: str,
    source_type: SourceType = SourceType.TRADE_PRESS,
) -> EvidenceClaim:
    return EvidenceClaim(
        text=text,
        source_url=f"https://example.com/{abs(hash(text)) % 10_000}",
        source_type=source_type,
        section=section,
    )


def strong(section: WebSection) -> list[EvidenceClaim]:
    """Three claims with an authoritative source — enough for HIGH."""
    return [
        claim(section, f"{section.value} registry fact", SourceType.OFFICIAL_REGISTRY),
        claim(section, f"{section.value} press fact", SourceType.TRADE_PRESS),
        claim(section, f"{section.value} more press", SourceType.TRADE_PRESS),
    ]


def build_deps(
    *,
    resolution: Resolution | None = None,
    extractor: ScriptedExtractor | None = None,
    tavily: FakeTavily | None = None,
    router: FakeRouter | None = None,
) -> AgentDependencies:
    return AgentDependencies(
        companies_house=FakeCompaniesHouse(resolution),  # type: ignore[arg-type]
        tavily=tavily or FakeTavily(),  # type: ignore[arg-type]
        router=router or FakeRouter(),  # type: ignore[arg-type]
        extractor=extractor or ScriptedExtractor(),
        synthesizer=_synthesize,
    )


@pytest.fixture(autouse=True)
def _patch_external(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the two gather functions and resolution at their import sites.

    The gathers are module-level functions rather than injected dependencies —
    that is Phase 2's shape, and rewriting it to suit the tests would be the
    tail wagging the dog.
    """

    async def fake_resolve(query: str, client):
        return client.resolution

    async def fake_registry(profile, client, **_):
        return RegistryGather(
            profile=profile,
            filings=[],
            officers=[],
            psc=[],
            failures=[],
        )

    async def fake_web(profile, client):
        gather = WebGather(company_name=profile.name)
        for section in WebSection:
            # Uses the real per-section depths rather than defaulting every
            # search to basic. Otherwise the fake's initial gather costs 3
            # credits where production costs 5, and any assertion about the
            # Phase 3.2 budget would be validating the fake's arithmetic.
            response = await client.search(
                f"{profile.name} {section.value}", depth=SECTION_DEPTH[section]
            )
            gather.sections.append(
                SectionSearch(
                    section=section,
                    query=response.query,
                    depth=response.depth,
                    results=response.results,
                    credits_spent=response.credits_spent,
                )
            )
        return gather

    monkeypatch.setattr("src.agent.nodes.resolve_company", fake_resolve)
    monkeypatch.setattr("src.agent.nodes.gather_companies_house", fake_registry)
    monkeypatch.setattr("src.agent.nodes.gather_web", fake_web)


def decisions_at(state, step: str) -> list:
    return [d for d in state.decisions if d.step == step]


# ---------------------------------------------------------------------------
# The happy path, and the branch NOT being taken
# ---------------------------------------------------------------------------


async def test_a_well_evidenced_company_never_re_searches() -> None:
    """The control. If this re-searched, every other test here would be
    meaningless — the branch would be firing unconditionally."""
    extractor = ScriptedExtractor({section: strong(section) for section in WebSection})
    deps = build_deps(extractor=extractor)

    state = await run_report("Monzo", deps)

    assert state.outcome is RunOutcome.COMPLETE
    assert state.report == "REPORT"
    assert state.researched_sections == []
    assert not decisions_at(state, RESEARCH)
    assert len(extractor.calls) == 1  # extracted once, never re-extracted
    assert all(a.band.value == "high" for a in state.assessments.values())


async def test_the_negative_decision_is_logged_for_every_section() -> None:
    """A log that only recorded the times the agent acted could not be audited
    for judgement. Phase 3.4 requires the reasoning for *not* re-searching."""
    extractor = ScriptedExtractor({section: strong(section) for section in WebSection})

    state = await run_report("Monzo", build_deps(extractor=extractor))

    evaluated = decisions_at(state, EVALUATE)
    assert len(evaluated) == len(WebSection)
    assert all(d.detail["re_search"] is False for d in evaluated)
    assert all("sufficiently evidenced" in d.outcome for d in evaluated)


# ---------------------------------------------------------------------------
# The branch being taken — and only for the section that needed it
# ---------------------------------------------------------------------------


async def test_only_the_under_evidenced_section_is_re_searched() -> None:
    extractor = ScriptedExtractor(
        first_pass={BUSINESS: strong(BUSINESS), COMPETITIVE: strong(COMPETITIVE)},
        second_pass={REPUTATION: strong(REPUTATION)},
    )
    tavily = FakeTavily()
    deps = build_deps(extractor=extractor, tavily=tavily)

    state = await run_report("Monzo", deps)

    assert state.researched_sections == [REPUTATION]
    # Three initial searches, then exactly one more.
    assert len(tavily.queries) == 4
    # The second extraction pass was narrowed to the re-searched section only.
    assert extractor.calls[1] == (REPUTATION,)


async def test_the_refined_query_differs_from_the_original_and_is_logged() -> None:
    """TASKS.md 3.5's second and third non-negotiables, together: the query must
    be demonstrably different, and the trigger, reasoning and refined query must
    all land in the 3.4 log."""
    extractor = ScriptedExtractor(
        first_pass={BUSINESS: strong(BUSINESS), COMPETITIVE: strong(COMPETITIVE)}
    )
    tavily = FakeTavily()

    state = await run_report("Monzo", build_deps(extractor=extractor, tavily=tavily))

    logged = decisions_at(state, RESEARCH)
    assert len(logged) == 1
    detail = logged[0].detail

    assert detail["section"] == REPUTATION.value
    assert detail["refined_query"] != detail["original_query"]
    assert detail["query_source"] == "model"
    assert detail["targets_gap"]
    assert detail["differs_because"]
    assert logged[0].rationale  # why it fired, not just that it did

    # And the query the log names is the query actually sent to Tavily.
    assert detail["refined_query"] in tavily.queries


async def test_a_re_search_that_finds_evidence_lifts_the_band() -> None:
    """End to end: thin section → re-search → new claims → higher band. Without
    this, the branch could fire and change nothing."""
    extractor = ScriptedExtractor(
        first_pass={BUSINESS: strong(BUSINESS), COMPETITIVE: strong(COMPETITIVE)},
        second_pass={REPUTATION: strong(REPUTATION)},
    )

    state = await run_report("Monzo", build_deps(extractor=extractor))

    assert state.assessments[REPUTATION].band.value == "high"
    assert len(state.claims) == 9


async def test_a_re_search_that_finds_nothing_still_terminates_honestly() -> None:
    """The loop is bounded by state, not by success. A section that stays thin
    reports LOW rather than being retried until it improves."""
    extractor = ScriptedExtractor(
        first_pass={BUSINESS: strong(BUSINESS), COMPETITIVE: strong(COMPETITIVE)},
        second_pass={},
    )

    state = await run_report("Monzo", build_deps(extractor=extractor))

    assert state.outcome is RunOutcome.COMPLETE
    assert state.assessments[REPUTATION].band.value == "low"
    assert state.researched_sections == [REPUTATION]


async def test_all_three_thin_sections_are_re_searched_within_the_cap() -> None:
    """The scope decision: one retry per under-evidenced section, capped at
    three, rather than one per report."""
    extractor = ScriptedExtractor(first_pass={}, second_pass={})
    tavily = FakeTavily()

    state = await run_report("Monzo", build_deps(extractor=extractor, tavily=tavily))

    assert len(state.researched_sections) == MAX_RESEARCH_SECTIONS
    assert len(tavily.queries) == 3 + MAX_RESEARCH_SECTIONS
    assert state.outcome is RunOutcome.COMPLETE


async def test_the_loop_runs_at_most_once_even_when_everything_stays_thin() -> None:
    """The stopping condition (3.2). Two gather rounds, two extractions, then
    synthesis — no third pass however bad the evidence is."""
    extractor = ScriptedExtractor(first_pass={}, second_pass={})

    state = await run_report("Monzo", build_deps(extractor=extractor))

    assert state.gather_rounds == 2
    assert len(extractor.calls) == 2
    assert state.outcome is RunOutcome.COMPLETE


# ---------------------------------------------------------------------------
# Declining to re-search for the right reasons
# ---------------------------------------------------------------------------


async def test_a_contradicted_section_is_not_re_searched() -> None:
    """LOW, but not thin. Spending a credit trying to out-vote a contradiction
    would work against Phase 4.3's rule that conflicts surface."""
    a = claim(REPUTATION, "dissolved", SourceType.OFFICIAL_REGISTRY)
    b = claim(REPUTATION, "trading", SourceType.TRADE_PRESS)
    extractor = ScriptedExtractor(
        first_pass={
            BUSINESS: strong(BUSINESS),
            COMPETITIVE: strong(COMPETITIVE),
            REPUTATION: [a, b],
        },
        conflicts=[
            Conflict(
                severity=ConflictSeverity.MAJOR,
                section=REPUTATION,
                claim_ids=[a.id, b.id],
                detail="register contradicts press",
            )
        ],
    )
    tavily = FakeTavily()

    state = await run_report("Monzo", build_deps(extractor=extractor, tavily=tavily))

    assert state.researched_sections == []
    assert len(tavily.queries) == 3  # no fourth search
    assert state.assessments[REPUTATION].reason.value == "major_conflict"

    logged = [d for d in decisions_at(state, EVALUATE) if d.detail["section"] == REPUTATION.value]
    assert "contradicted, not thin" in logged[0].outcome


async def test_a_provider_outage_during_refinement_falls_back_but_still_searches() -> None:
    """A re-search must not silently become conditional on the LLM being up."""

    class BrokenRouter:
        async def complete(self, *_: object, **__: object):
            raise RuntimeError("all providers failed")

    extractor = ScriptedExtractor(
        first_pass={BUSINESS: strong(BUSINESS), COMPETITIVE: strong(COMPETITIVE)}
    )
    tavily = FakeTavily()

    state = await run_report(
        "Monzo",
        build_deps(extractor=extractor, tavily=tavily, router=BrokenRouter()),  # type: ignore[arg-type]
    )

    logged = decisions_at(state, RESEARCH)[0]
    assert logged.detail["query_source"] == "fallback_template"
    assert "llm unavailable" in str(logged.detail["rejection_reason"])
    assert len(tavily.queries) == 4  # the search still happened


# ---------------------------------------------------------------------------
# Halting paths
# ---------------------------------------------------------------------------


async def test_an_ambiguous_name_halts_before_gathering() -> None:
    """Ambiguity is the disambiguation feature working, not an error — and no
    report is ever built about a company nobody confirmed."""
    resolution = Resolution(
        kind=ResolutionKind.AMBIGUOUS,
        query="monzo",
        candidates=[Candidate(profile=PROFILE, score=0.9, match="exact")],
    )
    tavily = FakeTavily()

    state = await run_report("monzo", build_deps(resolution=resolution, tavily=tavily))

    assert state.outcome is RunOutcome.NEEDS_INPUT
    assert state.report is None
    assert tavily.queries == []  # nothing was spent on an unconfirmed company


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        (ResolutionKind.NOT_FOUND, RunOutcome.NOT_FOUND),
        (ResolutionKind.INVALID, RunOutcome.INVALID_INPUT),
        (ResolutionKind.NEEDS_CONFIRMATION, RunOutcome.NEEDS_INPUT),
    ],
)
async def test_every_non_resolved_kind_maps_to_its_own_outcome(
    kind: ResolutionKind, expected: RunOutcome
) -> None:
    resolution = Resolution(kind=kind, query="x", profile=PROFILE)

    state = await run_report("x", build_deps(resolution=resolution))

    assert state.outcome is expected


async def test_an_llm_outage_during_extraction_is_not_reported_as_thin_evidence() -> None:
    """The distinction `config.py` exists to protect: 'our providers are down'
    must never reach the user wearing the same clothes as 'this company is
    thinly documented'."""
    from src.llm.router import AllProvidersFailedError

    class BrokenExtractor:
        async def __call__(self, evidence, sections):
            raise AllProvidersFailedError("primary and fallback both failed")

    deps = build_deps()
    deps.extractor = BrokenExtractor()

    state = await run_report("Monzo", deps)

    assert state.outcome is RunOutcome.PROVIDER_FAILURE
    assert state.outcome is not RunOutcome.NO_EVIDENCE
    assert "unavailable" in (state.error or "")


# ---------------------------------------------------------------------------
# The decision log as a whole
# ---------------------------------------------------------------------------


async def test_every_run_produces_an_ordered_readable_decision_log() -> None:
    extractor = ScriptedExtractor({section: strong(section) for section in WebSection})

    state = await run_report("Monzo", build_deps(extractor=extractor))

    steps = [d.step for d in state.decisions]
    assert steps[0] == "resolve"
    assert steps[-1] == "synthesize"
    assert all(d.rationale for d in state.decisions), "every decision states why"
    assert all(d.line() for d in state.decisions)


async def test_the_log_records_what_the_re_search_cost() -> None:
    """Phase 3.2's budget has to be inspectable after the fact, not just
    enforced during the run — otherwise 'the agent knew when to stop' is a
    claim with nothing behind it."""
    extractor = ScriptedExtractor(first_pass={}, second_pass={})

    state = await run_report("Monzo", build_deps(extractor=extractor))

    # Worst case is the baseline twice: every section re-searched, each at the
    # same depth its first pass used.
    assert state.credits_spent == 2 * BASELINE_CREDITS_PER_REPORT
    assert state.credits_remaining == 0
    assert state.may_research() is False
    assert "credits left" in state.budget_note()


async def test_each_section_is_re_searched_at_its_own_depth() -> None:
    """Pins the live finding: a re-search inherits its section's first-pass
    depth rather than defaulting to basic. Basic depth on a regulator-targeted
    reputation query returned Wikipedia and app-store pages; the same query at
    advanced depth returned the FCA complaints table and an ombudsman decision.
    """
    extractor = ScriptedExtractor(first_pass={}, second_pass={})
    tavily = FakeTavily()

    await run_report("Monzo", build_deps(extractor=extractor, tavily=tavily))

    re_search_depths = tavily.depths[len(WebSection) :]
    assert re_search_depths == [SECTION_DEPTH[section] for section in WebSection]
