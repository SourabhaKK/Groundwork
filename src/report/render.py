"""Deterministic report assembly (Phase 5.5, 5.6) — everything except the prose.

**The central decision of Phase 5 lives here.** The synthesis model writes
paragraphs. Every piece of *structure* — the confidence badge and the counts
inside it, the per-section source table, the citation numbering, the
excluded-from-scoring note, the evidence-gap list, the conflict block, and the
working table at the bottom — is rendered from data by this module.

Three things follow from that, and each of them is a requirement somewhere else
in the plan:

1. **Phase 5.7 becomes true by construction.** If Sonnet and Gemini only ever
   supply prose, they cannot produce differently-shaped reports. A cross-provider
   shape guarantee that rests on both models following formatting instructions
   is a guarantee that has to be re-earned every time a prompt changes.
2. **Phase 7.1a criterion 2 cannot drift.** "No section's confidence band is
   contradicted by its own listed evidence" holds because the badge and the
   working table both print from the same `ConfidenceAssessment` object the
   graph scored on. Nothing here counts claims a second time — see
   `_working_row`, which reads `assessment.total_claims` and
   `assessment.scored_claims` rather than measuring the claim list it was handed.
   The Phase 0.7 mock's own badge once conflated claim count with distinct-source
   count, and that was a human rendering by hand.
3. **A citation cannot be invented.** Numbers come from `citations.py`, which
   derives them from claims.

**What this module deliberately does not do:** it does not soften, suppress or
second-guess anything `score_confidence()` returned. Phase 4 disclosed two
limitations — filing-history claims over-enumerating on noisy register
documents, and a denominator mismatch occasionally read as a MAJOR conflict —
and both will show up here, in a badge count or in a LOW band. Rendering them is
the point. A report that quietly disagreed with the rule it publishes would be
worse than one that shows a known rough edge, and Phase 7.1a exists to find
exactly these.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from src.agent.claims import Conflict, ConflictSeverity, EvidenceClaim
from src.data_sources.failures import GatherFailure
from src.data_sources.sources import SourceType
from src.data_sources.web_gather import WebSection
from src.report.citations import SectionCitations
from src.report.confidence import ConfidenceAssessment, ConfidenceBand, ConfidenceReason

#: Section order and headings, fixed to the Phase 0.7 mock. Phase 7.1 diffs the
#: real output against that document, so these strings are an interface.
SECTION_TITLES: dict[WebSection, str] = {
    WebSection.BUSINESS_MODEL: "Business Model Summary",
    WebSection.COMPETITIVE_LANDSCAPE: "Competitive Landscape Assessment",
    WebSection.REPUTATION: "Company Quality Signals",
}

SECTION_ORDER: tuple[WebSection, ...] = (
    WebSection.BUSINESS_MODEL,
    WebSection.COMPETITIVE_LANDSCAPE,
    WebSection.REPUTATION,
)

#: TASKS.md 5.3's signal-strength rating, in the quality-signals section's own
#: words. Derived from the band, never judged separately.
_SIGNAL_STRENGTH: dict[ConfidenceBand, str] = {
    ConfidenceBand.HIGH: "strong",
    ConfidenceBand.MEDIUM: "moderate",
    ConfidenceBand.LOW: "weak",
}

_BAND_EMOJI: dict[ConfidenceBand, str] = {
    ConfidenceBand.HIGH: "🟢",
    ConfidenceBand.MEDIUM: "🟡",
    ConfidenceBand.LOW: "🔴",
}

#: Why the band is what it is, in the badge, in one clause. Phrased so a reader
#: who never opens TASKS.md can tell a thin section from a contradicted one —
#: they are both LOW and they mean completely different things to an underwriter.
_REASON_CLAUSE: dict[ConfidenceReason, str] = {
    ConfidenceReason.NO_CLAIMS: (
        "no claim in this section scores, so this band reports an absence of usable "
        "evidence rather than an adverse finding"
    ),
    ConfidenceReason.MAJOR_CONFLICT: (
        "held at LOW by a MAJOR conflict between scored claims — the sources contradict "
        "each other on a core fact, and the disagreement is reported rather than resolved"
    ),
    ConfidenceReason.WEAK_SOURCES: (
        "held at LOW on source authority: the scoring evidence is general web material "
        "only, or a single review"
    ),
    ConfidenceReason.MINOR_CONFLICT: (
        "capped at MEDIUM by a MINOR conflict — the sources agree on the core fact but "
        "differ on detail, and more sources cannot outvote that"
    ),
    ConfidenceReason.CORROBORATED: (
        "count-and-authority gate met: at least three scored claims with independent or "
        "trade-press corroboration"
    ),
    ConfidenceReason.SUFFICIENT: (
        "scored on evidence that is real but does not meet the count-and-authority gate for HIGH"
    ),
}

#: Phase 2.3a's tiers, most authoritative first. Written out rather than derived
#: from the enum's declaration order so that reordering the enum for an unrelated
#: reason cannot silently change what a report calls its "highest scored tier".
_TIER_AUTHORITY: tuple[SourceType, ...] = (
    SourceType.OFFICIAL_REGISTRY,
    SourceType.OFFICIAL_SELF,
    SourceType.TRADE_PRESS,
    SourceType.REVIEW,
    SourceType.GENERAL_WEB,
)

#: The order `score_confidence` checks its conditions in. Used only to say
#: "(not reached)" in the working table for conditions that never ran, so the
#: table shows the precedence the rule actually applies rather than implying
#: every condition was evaluated.
_PRECEDENCE: tuple[ConfidenceReason, ...] = (
    ConfidenceReason.NO_CLAIMS,
    ConfidenceReason.MAJOR_CONFLICT,
    ConfidenceReason.WEAK_SOURCES,
    ConfidenceReason.MINOR_CONFLICT,
    ConfidenceReason.CORROBORATED,
)


@dataclass
class SectionRender:
    """Everything one section needs, with the prose already generated.

    Assembled by `synthesize.py` and rendered here. Split this way so the
    renderer is a pure function of data — every test below can build one of
    these by hand without a model call.
    """

    section: WebSection
    assessment: ConfidenceAssessment
    claims: Sequence[EvidenceClaim]
    citations: SectionCitations
    prose: str
    gaps: Sequence[str] = ()
    conflicts: Sequence[Conflict] = ()
    failures: Sequence[GatherFailure] = ()
    warnings: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return SECTION_TITLES[self.section]

    @property
    def scored(self) -> list[EvidenceClaim]:
        return [claim for claim in self.claims if claim.counts_toward_confidence]

    @property
    def excluded(self) -> list[EvidenceClaim]:
        return [claim for claim in self.claims if not claim.counts_toward_confidence]


def _format_date(value: datetime | None) -> str:
    return value.strftime("%d %b %Y") if value else "—"


def _tier_summary(claims: Sequence[EvidenceClaim]) -> str:
    """Which authority tiers the *scoring* evidence actually came from.

    Counted over distinct sources rather than claims, and labelled as sources,
    because the badge's leading number is already a claim count and the two must
    never be confusable again — that conflation is one of the five spec fixes
    Phase 0.7 produced.
    """
    by_tier: dict[str, set[str]] = {}
    for claim in claims:
        by_tier.setdefault(claim.source_type.value, set()).add(claim.source_url)
    if not by_tier:
        return "no scoring sources"
    parts = [
        f"{len(urls)} `{tier}`"
        for tier, urls in sorted(by_tier.items(), key=lambda item: (-len(item[1]), item[0]))
    ]
    return "including " + ", ".join(parts)


def render_badge(render: SectionRender) -> str:
    """The badge, before the prose — never a disclaimer at the end.

    Placement is a product decision, not a layout one: the band has to be the
    first thing read, so a reader cannot absorb three confident-sounding
    paragraphs and only then learn the evidence was thin.

    Counts come from the `ConfidenceAssessment`. Nothing here re-derives them.
    """
    assessment = render.assessment
    emoji = _BAND_EMOJI[assessment.band]
    sources = len(render.citations)
    clause = _REASON_CLAUSE[assessment.reason]
    # TASKS.md 5.3 asks the quality-signals section for an explicit
    # signal-strength rating, and the mock puts it at the head of that badge.
    # It is a restatement of the band in the section's own vocabulary, derived
    # from the band rather than judged separately — two numbers saying the same
    # thing is how they start disagreeing.
    strength = (
        f"Signal strength: {_SIGNAL_STRENGTH[assessment.band]}. "
        if render.section is WebSection.REPUTATION
        else ""
    )
    return (
        f"> ### {emoji} {assessment.band.value.upper()}\n"
        f"> **{strength}{assessment.total_claims} claims ({assessment.scored_claims} scored) "
        f"across {sources} source{'s' if sources != 1 else ''}, "
        f"{_tier_summary(render.scored)} — {clause}.**"
    )


def render_conflicts(render: SectionRender) -> str:
    """Every detected conflict, both sides kept.

    Phase 4.3's rule is that a disagreement between sources is information the
    underwriter needs, not noise to clean up before they see it. A conflict is
    rendered whether or not it moved the band — an excluded claim's disagreement
    still reaches the reader, and Phase 4 carried that forward explicitly as
    something Phase 5.5's badge must not imply otherwise.
    """
    blocks = []
    for conflict in render.conflicts:
        severity = conflict.severity.value.upper()
        drives = (
            " (drives the band)"
            if (
                conflict.severity is ConflictSeverity.MAJOR
                and render.assessment.reason is ConfidenceReason.MAJOR_CONFLICT
            )
            or (
                conflict.severity is ConflictSeverity.MINOR
                and render.assessment.reason is ConfidenceReason.MINOR_CONFLICT
            )
            else " (recorded; did not move the band)"
        )
        blocks.append(f"**⚠️ Conflict detected — {severity}{drives}.** {conflict.detail}")
    return "\n\n".join(blocks)


def render_gaps(render: SectionRender) -> str:
    """What the evidence does not establish, stated rather than smoothed over.

    Two sources, deliberately merged into one list. The model contributes gaps it
    can see in the claims; `GatherFailure`s contribute sources that could not be
    fetched at all. Phase 2 made the gathering layer partial-tolerant on the
    condition that its failures would be *rendered* — a bundle whose failures are
    dropped is strictly worse than one that failed loudly.
    """
    bullets = [f"- {gap.strip()}" for gap in render.gaps if gap.strip()]
    bullets += [
        f"- **{failure.source} could not be retrieved** ({failure.kind.value}): {failure.detail}"
        for failure in render.failures
    ]
    if not bullets:
        return ""
    return "**⚠️ Evidence gaps, stated rather than smoothed over:**\n\n" + "\n".join(bullets)


def render_sources_table(render: SectionRender, number: int) -> str:
    """The per-section source table, with the Phase 2.3a tier on every row."""
    lines = [
        f"### Sources — Section {number}",
        "",
        "| # | Source | Type | Retrieved |",
        "|---|---|---|---|",
    ]
    for citation in render.citations.citations:
        lines.append(
            f"| {citation.number} | {citation.label} | `{citation.source_type.value}` | "
            f"{_format_date(citation.retrieved_at)} |"
        )
    if not len(render.citations):
        lines.append("| — | *No source produced a usable claim for this section.* | — | — |")
    return "\n".join(lines)


def render_exclusions(render: SectionRender) -> str:
    """The excluded-claims note, built from the claims' own `exclusion_reason`.

    Phase 4's live pass excluded 19 of 104 claims on quote verification alone,
    plus 14 more to the over-fragmentation guard. If those simply vanished, the
    report would look thinner than the evidence gathered *and the reason for the
    thinness would be invisible* — which is worse than the honest thinness the
    band is reporting. So the facts stay in the prose above and their exclusion
    is stated here, per claim.
    """
    excluded = render.excluded
    if not excluded:
        return ""
    lines = ["**Excluded from scoring (`counts_toward_confidence = False`):**", ""]
    for claim in excluded:
        marker = render.citations.marker_for(claim.source_url)
        reason = claim.exclusion_reason or "no reason recorded"
        lines.append(f"- {marker} {claim.text} — *excluded: {reason}*".strip())
    lines.append("")
    lines.append(
        "These remain in the prose above because they are informative; they simply do "
        "not count toward this section's band."
    )
    return "\n".join(lines)


def render_section(render: SectionRender, number: int) -> str:
    """One complete section, in the Phase 0.7 mock's order.

    Heading → badge → prose → conflicts → gaps → sources → exclusions.
    """
    blocks = [
        f"## {number}. {render.title}",
        render_badge(render),
        render.prose.strip(),
        render_conflicts(render),
        render_gaps(render),
        render_sources_table(render, number),
        render_exclusions(render),
    ]
    return "\n\n".join(block for block in blocks if block.strip())


def _reached(reason: ConfidenceReason) -> int:
    """How far down the precedence order evaluation got before returning.

    `SUFFICIENT` is the fall-through: every condition was checked and none
    matched, so it sits past the end of the list.
    """
    return _PRECEDENCE.index(reason) if reason in _PRECEDENCE else len(_PRECEDENCE)


def _condition_cell(reason: ConfidenceReason, condition: ConfidenceReason, fired: str) -> str:
    """One boolean column of the working table, honouring precedence.

    A condition after the one that actually fired was never evaluated, and
    printing "No" for it would misrepresent the rule as checking everything.
    """
    if reason is condition:
        return fired
    return "No" if _PRECEDENCE.index(condition) < _reached(reason) else "(not reached)"


def _working_row(number: int, render: SectionRender) -> str:
    """One section's line of the working table.

    **`Claims` and `Scored` are read from the `ConfidenceAssessment`**, the same
    object `render_badge` prints from — not recounted off the claim list. The
    working table exists so a reader can check a band against its own evidence,
    and a table that measured the claims independently could disagree with the
    badge above it while both were "correct". One number, one source.
    """
    assessment = render.assessment
    reason = assessment.reason
    tiers = {claim.source_type for claim in render.scored}
    highest = next((tier.value for tier in _TIER_AUTHORITY if tier in tiers), "—")
    gate = (
        "Met"
        if reason is ConfidenceReason.CORROBORATED
        else ("Not met" if reason is ConfidenceReason.SUFFICIENT else "(not reached)")
    )
    return (
        f"| {number}. {render.title} | {assessment.total_claims} | {assessment.scored_claims} | "
        f"{len(render.citations)} | `{highest}` | "
        f"{_condition_cell(reason, ConfidenceReason.MAJOR_CONFLICT, 'Yes')} | "
        f"{_condition_cell(reason, ConfidenceReason.WEAK_SOURCES, 'Yes')} | "
        f"{_condition_cell(reason, ConfidenceReason.MINOR_CONFLICT, 'Yes')} | "
        f"{gate} | "
        f"**{_BAND_EMOJI[assessment.band]} {assessment.band.value.upper()}** |"
    )


def render_working_table(renders: Sequence[SectionRender]) -> str:
    """The bottom working table — the per-section audit trail for the rule.

    Kept from the mock rather than treated as a mock-authoring artifact. It is
    what lets a reader verify Phase 7.1a's second criterion themselves instead of
    trusting the badge, and it shows the precedence the rule applies, which is
    the fix that came out of the Phase 0.7 build.

    Distinct sources are shown alongside claim counts so the two can never be
    confused again — the rule scores on **claims**.
    """
    lines = [
        "## Confidence Scoring Applied (working, for verification)",
        "",
        "Per Phase 5.4, evaluated per section, conditions checked strictly in order — the "
        "first match returns. `Claims` counts every claim in the section; `Scored` counts "
        "only those with `counts_toward_confidence = True`. Both numbers are the ones the "
        "scoring function itself returned, not a recount.",
        "",
        (
            "| Section | Claims | Scored | Distinct sources | Highest scored tier | MAJOR? | "
            "All `general_web` / lone `review`? | MINOR? | Count + authority gate | **Band** |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    lines += [_working_row(index, render) for index, render in enumerate(renders, start=1)]
    return "\n".join(lines)


def render_header(
    company_name: str,
    company_number: str,
    generated_at: datetime,
    resolution_line: str,
) -> str:
    return "\n".join(
        [
            f"# Underwriting Intelligence Report — {company_name.title()}",
            "",
            f"**Subject:** {company_name} · Companies House no. **{company_number}**",
            f"**Report generated:** {_format_date(generated_at)}",
            f"**Resolution:** {resolution_line}",
        ]
    )


def render_report(
    *,
    company_name: str,
    company_number: str,
    generated_at: datetime,
    resolution_line: str,
    renders: Sequence[SectionRender],
) -> str:
    """The whole report.

    No summary table at the top and no disclaimer at the end — both deliberate,
    both matching the mock. Uncertainty belongs per section, next to the evidence
    it is about; an end-of-report disclaimer is the thing TASKS.md 5.5
    specifically rules out, because it lets every section read as equally
    confident until the reader reaches the bottom.
    """
    parts = [render_header(company_name, company_number, generated_at, resolution_line)]
    parts += [render_section(render, number) for number, render in enumerate(renders, start=1)]
    parts.append(render_working_table(renders))
    return "\n\n---\n\n".join(parts) + "\n"


__all__ = [
    "SECTION_ORDER",
    "SECTION_TITLES",
    "SectionRender",
    "render_badge",
    "render_conflicts",
    "render_exclusions",
    "render_gaps",
    "render_header",
    "render_report",
    "render_section",
    "render_sources_table",
    "render_working_table",
]
