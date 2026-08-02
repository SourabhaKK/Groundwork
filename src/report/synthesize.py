"""Phase 5.1-5.3 — the `Synthesizer` the graph has been calling since Phase 3.

Implements the seam `AgentDependencies.synthesizer` left open. The graph is
unchanged, exactly as Phase 4 left `AgentDependencies.extractor` unchanged.

**Three calls, one per section, run concurrently.** Per section rather than per
report because the band is a per-section fact and the prose has to be consistent
with its own band — one call holding all three bands in context is how Section
3's HIGH language ends up in Section 1's MEDIUM prose. Concurrently because the
cross-section context digest is computed from claims and assessments before any
call, so no section's prompt waits on another section's output.

**Synthesis is Sonnet-only** (`LLMTier.SYNTHESIS`). Extraction was Haiku. The
router picks the model from the tier and callers never name one, so the tiering
holds structurally — but the prompts have to respect it too, which is why
`prompts.py` asks for judgement about existing claims rather than for facts.

**A section that fails does not disappear.** If one section's call fails for a
reason short of total provider failure, that section still renders — with its
real band, its real sources, and a plain statement that its prose could not be
generated. A missing section would silently shorten the report and take its
evidence gaps with it. `AllProvidersFailedError` is the exception and propagates:
`synthesize_node` turns it into a PROVIDER_FAILURE halt, because an outage
rendered as a thin report is the one failure this project refuses.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import ValidationError

from src.agent.claims import Conflict, EvidenceClaim
from src.agent.resolve import Resolution, ResolutionKind
from src.data_sources.failures import GatherFailure
from src.data_sources.sources import EvidenceBundle, TaggedDocument
from src.data_sources.web_gather import WebSection
from src.llm.json_parsing import json_payload
from src.llm.router import AllProvidersFailedError, LLMRouter, LLMTier
from src.report import prompts
from src.report.citations import build_citations, validate_markers
from src.report.confidence import ConfidenceAssessment, score_confidence
from src.report.render import SECTION_ORDER, SectionRender, render_report

logger = logging.getLogger(__name__)

#: What a section says when its prose could not be generated. Deliberately not a
#: hedge or an apology: the reader needs to know the absence is ours, not the
#: evidence's, because "we could not write this up" and "there was nothing to
#: write up" are the two things this whole report exists to keep apart.
_PROSE_FAILED = (
    "*Synthesis for this section did not complete. The confidence band, sources and "
    "evidence gaps below are still computed from the claims that were extracted — "
    "the missing element is the written summary, not the evidence.*"
)

_RESOLUTION_LINES: dict[ResolutionKind, str] = {
    ResolutionKind.RESOLVED: "Exact match on registered name. No disambiguation required.",
    ResolutionKind.NEEDS_CONFIRMATION: (
        "One company identified, but not accepted silently — see the reasons recorded on the run."
    ),
}


def resolution_line(resolution: Resolution | None) -> str:
    """The header's one-line account of how this company was identified.

    Rendered because disambiguation is a graded feature and a report that does
    not say which of several possible companies it is about is not traceable.
    """
    if resolution is None:
        return "Not recorded."
    if resolution.kind is ResolutionKind.NEEDS_CONFIRMATION and resolution.reasons:
        reasons = ", ".join(reason.value for reason in resolution.reasons)
        return f"One company identified, but flagged for confirmation ({reasons})."
    return _RESOLUTION_LINES.get(resolution.kind) or resolution.detail or resolution.kind.value


def _by_section(
    claims: Sequence[EvidenceClaim],
) -> dict[WebSection, list[EvidenceClaim]]:
    grouped: dict[WebSection, list[EvidenceClaim]] = {section: [] for section in SECTION_ORDER}
    for claim in claims:
        grouped.setdefault(claim.section, []).append(claim)
    return grouped


def _conflicts_by_section(conflicts: Sequence[Conflict]) -> dict[WebSection, list[Conflict]]:
    grouped: dict[WebSection, list[Conflict]] = {section: [] for section in SECTION_ORDER}
    for conflict in conflicts:
        grouped.setdefault(conflict.section, []).append(conflict)
    return grouped


def _failures_for(section: WebSection, failures: Sequence[GatherFailure]) -> list[GatherFailure]:
    """Which gather failures this section has to disclose.

    A failed web search belongs to the section it was run for. A failed register
    endpoint belongs to *every* section, because `EvidenceBundle.for_section`
    hands register documents to all three — incorporation data bears on the
    business model, officer churn on quality signals. Attributing a missing
    officers list to one section only would leave the other two silently
    thinner with no stated reason.
    """
    web_sections = {member.value for member in WebSection}
    return [
        failure
        for failure in failures
        if failure.source == section.value or failure.source not in web_sections
    ]


def _context_digest(
    section: WebSection,
    assessments: dict[WebSection, ConfidenceAssessment],
    grouped: dict[WebSection, list[EvidenceClaim]],
    limit: int = 3,
) -> list[str]:
    """Bands and a few headline facts from the *other* sections.

    Deliberately small and read-only. It exists so a section can notice a
    connection the mock's Section 3 makes by hand — a complaint theme plausibly
    being the customer-facing surface of a regulatory finding — without being
    able to cite across sections, where the numbering differs.
    """
    lines: list[str] = []
    for other in SECTION_ORDER:
        if other is section:
            continue
        assessment = assessments.get(other)
        band = assessment.band.value.upper() if assessment else "not scored"
        headline = [
            claim.text for claim in grouped.get(other, []) if claim.counts_toward_confidence
        ][:limit]
        lines.append(
            f"{other.value} — band {band}" + (f"; e.g. {' | '.join(headline)}" if headline else "")
        )
    return lines


class ReportSynthesizer:
    """Phase 5's implementation of the `Synthesizer` protocol.

    Stateless apart from the router, so one instance serves every run — same
    shape as `ClaimExtractor`.
    """

    def __init__(self, router: LLMRouter, *, max_tokens: int = prompts.MAX_SECTION_TOKENS) -> None:
        self._router = router
        self._max_tokens = max_tokens

    async def __call__(self, state: object) -> str:
        """Render the whole report from graph state."""
        claims: list[EvidenceClaim] = list(getattr(state, "claims", []) or [])
        conflicts: list[Conflict] = list(getattr(state, "conflicts", []) or [])
        assessments: dict[WebSection, ConfidenceAssessment] = dict(
            getattr(state, "assessments", {}) or {}
        )
        evidence: EvidenceBundle | None = getattr(state, "evidence", None)
        profile = getattr(state, "profile", None)

        documents: list[TaggedDocument] = list(evidence.documents) if evidence else []
        failures: list[GatherFailure] = list(evidence.failures) if evidence else []

        grouped = _by_section(claims)
        grouped_conflicts = _conflicts_by_section(conflicts)

        for section in SECTION_ORDER:
            if section not in assessments:
                # The graph always scores before synthesis; this is the safety
                # net for a caller that didn't. It calls the same function the
                # graph does, so the two cannot disagree — re-deriving a band
                # with a second rule is precisely what Phase 5.4 forbids.
                assessments[section] = score_confidence(
                    grouped.get(section, []), grouped_conflicts.get(section, [])
                )

        renders = await asyncio.gather(
            *(
                self._section(
                    section=section,
                    company=getattr(profile, "name", "")
                    or (evidence.company_name if evidence else ""),
                    number=getattr(profile, "number", "")
                    or (evidence.company_number if evidence else ""),
                    assessment=assessments[section],
                    claims=grouped.get(section, []),
                    conflicts=grouped_conflicts.get(section, []),
                    failures=_failures_for(section, failures),
                    documents=documents,
                    context=_context_digest(section, assessments, grouped),
                )
                for section in SECTION_ORDER
            )
        )

        company_name = (
            getattr(profile, "name", None)
            or (evidence.company_name if evidence else "")
            or "Unknown"
        )
        company_number = (
            getattr(profile, "number", None) or (evidence.company_number if evidence else "") or "—"
        )
        return render_report(
            company_name=company_name,
            company_number=company_number,
            generated_at=datetime.now(UTC),
            resolution_line=resolution_line(getattr(state, "resolution", None)),
            renders=list(renders),
        )

    async def _section(
        self,
        *,
        section: WebSection,
        company: str,
        number: str,
        assessment: ConfidenceAssessment,
        claims: Sequence[EvidenceClaim],
        conflicts: Sequence[Conflict],
        failures: Sequence[GatherFailure],
        documents: Sequence[TaggedDocument],
        context: Sequence[str],
    ) -> SectionRender:
        citations = build_citations(claims, documents)
        warnings: list[str] = []

        prose, gaps = await self._write(
            section=section,
            company=company,
            number=number,
            assessment=assessment,
            claims=claims,
            citations=citations,
            conflicts=conflicts,
            context=context,
            warnings=warnings,
        )

        # Phase 5.6's guard. The prompt asks the model to carry through the
        # marker already bound to each claim, but a prompt is a request: `[5]` in
        # a four-source section is either a miscount or an invention, and both
        # render as a citation an underwriter would click and not find.
        valid = citations.numbers
        prose, stripped = validate_markers(prose, valid)
        warnings += stripped
        cleaned_gaps = []
        for gap in gaps:
            text, gap_warnings = validate_markers(gap, valid)
            warnings += gap_warnings
            cleaned_gaps.append(text)

        if warnings:
            logger.warning("synthesis %s: %s", section.value, "; ".join(warnings))

        return SectionRender(
            section=section,
            assessment=assessment,
            claims=list(claims),
            citations=citations,
            prose=prose,
            gaps=cleaned_gaps,
            conflicts=list(conflicts),
            failures=list(failures),
            warnings=warnings,
        )

    async def _write(
        self,
        *,
        section: WebSection,
        company: str,
        number: str,
        assessment: ConfidenceAssessment,
        claims: Sequence[EvidenceClaim],
        citations: object,
        conflicts: Sequence[Conflict],
        context: Sequence[str],
        warnings: list[str],
    ) -> tuple[str, list[str]]:
        """One section's prose, or an honest placeholder and a recorded reason."""
        prompt = prompts.build_prompt(
            section=section,
            company=company,
            number=number,
            assessment=assessment,
            claims=claims,
            citations=citations,  # type: ignore[arg-type]
            conflicts=conflicts,
            context=context,
        )
        try:
            response = await self._router.complete(
                prompt,
                tier=LLMTier.SYNTHESIS,
                system=prompts.SYSTEM,
                max_tokens=self._max_tokens,
            )
        except AllProvidersFailedError:
            # Never degraded to a thin-looking section. The graph halts on this.
            raise
        except Exception as exc:  # noqa: BLE001 — one section failing is survivable
            logger.warning("synthesis %s: failed (%s: %s)", section.value, type(exc).__name__, exc)
            warnings.append(f"section call failed: {type(exc).__name__}")
            return _PROSE_FAILED, []

        payload = json_payload(response.text)
        if payload is None:
            warnings.append("no JSON object in the response")
            return _PROSE_FAILED, []
        try:
            parsed = prompts.SectionProse.model_validate(payload)
        except ValidationError as exc:
            warnings.append(f"unusable JSON ({exc.error_count()} error(s))")
            return _PROSE_FAILED, []

        if not parsed.prose.strip():
            warnings.append("empty prose in an otherwise valid response")
            return _PROSE_FAILED, list(parsed.gaps)

        # `gaps` has a list default, so a response that omits the field entirely
        # validates cleanly and renders a section with no evidence-gaps block —
        # indistinguishable from a section that genuinely had nothing to state.
        # Measured on the fallback at the old 2,000-token cap: under budget
        # pressure Gemini dropped the whole `gaps` array and still closed valid
        # JSON. Losing the gaps block silently is the worst direction for this
        # report to fail in, since that block is where the uncertainty the whole
        # design exists to surface actually gets written down.
        if isinstance(payload, dict) and "gaps" not in payload:
            warnings.append("response omitted the gaps field entirely")

        logger.info(
            "synthesis %s: %d chars via %s/%s%s",
            section.value,
            len(parsed.prose),
            response.provider,
            response.model,
            " (FALLBACK)" if response.used_fallback else "",
        )
        return parsed.prose, list(parsed.gaps)


__all__ = ["ReportSynthesizer", "resolution_line"]
