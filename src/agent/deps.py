"""What the graph needs from the outside world (Phase 3.1).

Collected into one injected object rather than reached for via module-level
singletons inside nodes. Two reasons, both practical: the graph tests substitute
fakes for every external call without monkeypatching, and Phase 4 and 5 can swap
their real implementations in without touching the graph's wiring.

The extractor and synthesizer are protocols with deliberate placeholder
implementations. Phase 3's job is the control flow; what happens inside `extract`
is Phase 4's and what happens inside `synthesize` is Phase 5's. The placeholders
exist so the graph runs end to end today — and they are loud about being
placeholders rather than quietly returning something plausible.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from src.agent.claims import ExtractionResult
from src.agent.extract import ClaimExtractor
from src.data_sources.companies_house import CompaniesHouseClient
from src.data_sources.sources import EvidenceBundle
from src.data_sources.tavily import TavilyClient
from src.data_sources.web_gather import WebSection
from src.llm.router import LLMRouter, get_router
from src.report.synthesize import ReportSynthesizer


class Extractor(Protocol):
    """Phase 4.2 will implement this."""

    async def __call__(
        self, evidence: EvidenceBundle, sections: Sequence[WebSection]
    ) -> ExtractionResult:
        """Extract claims for `sections` from `evidence`.

        `sections` is narrowed on Phase 3.5's second pass to only the sections
        that were re-searched — re-extracting all three would spend tokens
        re-deriving claims the state already holds.
        """
        ...


class Synthesizer(Protocol):
    """Phase 5.1-5.3 will implement this."""

    async def __call__(self, state: object) -> str: ...


class _NotYetExtracting:
    """Placeholder extractor — returns nothing, and says so in the log.

    Returning an empty result is the honest placeholder: it makes every section
    score LOW/NO_CLAIMS, which is exactly what "no extraction implemented yet"
    means. Returning invented claims to make the graph look healthier would put
    a fabrication in the one pipeline whose entire purpose is grounding.
    """

    phase = "4.2"

    async def __call__(
        self, evidence: EvidenceBundle, sections: Sequence[WebSection]
    ) -> ExtractionResult:
        return ExtractionResult()


class _NotYetSynthesizing:
    phase = "5.1-5.3"

    async def __call__(self, state: object) -> str:
        return "Report synthesis lands in Phase 5."


@dataclass
class AgentDependencies:
    """Everything the graph calls out to."""

    companies_house: CompaniesHouseClient
    tavily: TavilyClient
    router: LLMRouter
    extractor: Extractor
    synthesizer: Synthesizer

    @classmethod
    def build(
        cls,
        *,
        companies_house: CompaniesHouseClient | None = None,
        tavily: TavilyClient | None = None,
        router: LLMRouter | None = None,
        extractor: Extractor | None = None,
        synthesizer: Synthesizer | None = None,
    ) -> AgentDependencies:
        # Built before the extractor, which needs it. Phase 4 replaced the
        # placeholder extractor with the real one; `_NotYetExtracting` is kept
        # because the graph tests still use it to exercise the no-claims path
        # deterministically, and because `extraction_is_placeholder` remains the
        # honest thing for the decision log to say when it is in use.
        resolved_router = router or get_router()
        return cls(
            companies_house=companies_house or CompaniesHouseClient(),
            tavily=tavily or TavilyClient(),
            router=resolved_router,
            extractor=extractor or ClaimExtractor(resolved_router),
            # Phase 5 replaced the placeholder synthesizer with the real one, on
            # the same terms as Phase 4 replaced the extractor: the seam and the
            # graph are unchanged. `_NotYetSynthesizing` stays for the graph
            # tests, which need a synthesizer that cannot call a model.
            synthesizer=synthesizer or ReportSynthesizer(resolved_router),
        )

    @property
    def extraction_is_placeholder(self) -> bool:
        return isinstance(self.extractor, _NotYetExtracting)


__all__ = ["AgentDependencies", "Extractor", "Synthesizer"]
