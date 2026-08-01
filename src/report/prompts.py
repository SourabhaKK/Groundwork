"""Synthesis prompts (Phase 5.1-5.3), written for the SYNTHESIS tier.

**These are not extraction prompts and must not be written like them.** Phase
4.2's prompts run on Haiku, one call per document, and ask for atomic facts in
JSON with a verbatim quote each. These run on Sonnet, one call per *section*,
over claims that have already been extracted, verified and scored — the job here
is judgement about evidence that already exists, not retrieval of new facts. The
tiering is a `TECH_STACK.md` lock and the router enforces it (callers name a
tier, never a model), so the only way to blur it is to write a prompt that asks
the synthesis model to do extraction's job. Don't.

**The model writes prose and nothing else.** No headings, no tables, no badge,
no source list — all of that is rendered from data in `render.py`. That split is
what makes Phase 5.7's cross-provider guarantee structural rather than a hope
that two model families follow formatting instructions the same way.

**The model never sees a URL.** Each claim arrives with the citation marker
already bound to it by `citations.py`; the model's only job with a citation is
to carry it through. There is no mapping in the prompt it could extend, which is
the same reason Phase 4.2 attached provenance from the `TaggedDocument` instead
of letting the response carry a `source_url`.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, Field

from src.agent.claims import Conflict, EvidenceClaim
from src.data_sources.web_gather import WebSection
from src.report.citations import SectionCitations
from src.report.confidence import ConfidenceAssessment

#: Per-section output cap. Small on purpose. Three modest calls survive the
#: fallback's tighter effective budget far better than one large call — Phase 3
#: measured Gemini 2.5 spending 602 tokens on internal reasoning against 15
#: visible before `thinking_budget=0` was set, and a fallback's job is to hold
#: the report's shape, not to reason harder than the model it stands in for.
MAX_SECTION_TOKENS = 2_000

SYSTEM = (
    "You write sections of a UK commercial underwriting report. You are given a "
    "fixed list of verified claims and you write only what those claims support. "
    "You never introduce a fact that is not in the list, never soften or "
    "overstate what a claim says, and never contradict the confidence band you "
    "are given — the band was computed by a scoring rule, not by you, and it is "
    "not yours to argue with. You return only JSON."
)

#: What each section is for. Written as the question an underwriter is actually
#: asking, because "summarise the business model" produces a description and
#: what is wanted is an assessment.
_BRIEFS: dict[WebSection, str] = {
    WebSection.BUSINESS_MODEL: """\
SECTION: Business Model Summary.

Answer, from the claims only: how does this company make money, who from, and at
what scale? Cover the revenue model, the customer base, and any figures that
indicate size or trajectory — reproducing figures exactly as the claim states
them, never rounded or re-based.

Where a figure is the company's own disclosure rather than an independently
filed or reported one, say so in the sentence that uses it. That distinction is
the whole reason the source taxonomy separates a regulator from a company's own
account of itself, and an underwriter reading a revenue number needs to know
which one they are looking at.""",
    WebSection.COMPETITIVE_LANDSCAPE: """\
SECTION: Competitive Landscape Assessment.

This must be a structured assessment of competitive INTENSITY, not a list of
competitors. A list of names is the failure mode here.

State how intense competition in this company's market is and what specifically
in the claims supports that reading. Then state what it means for a lender
rather than for a market analyst — whether the company's position looks
company-specific or is just the whole sector moving together, and where that
locates the risk. If the claims only support a comparison against one or two
named competitors, say that the assessment rests on that comparison and do not
generalise it into a market-wide statement.""",
    WebSection.REPUTATION: """\
SECTION: Company Quality Signals.

Cover governance, regulatory history, filing discipline and customer sentiment,
as far as the claims support each.

Read the band correctly and make sure the reader does too: it rates the STRENGTH
OF THE EVIDENCE, not the favourability of the finding. A well-documented adverse
finding is high-confidence evidence of a problem, and must be reported as
clearly as a positive one — never soften it because the band is high. Where a
negative finding has documented mitigating context (a completed remediation, a
historic date range), give both.""",
}

_RULES = """\
RULES — these are what the report's trustworthiness rests on.

1. Use ONLY the claims below. If a fact is not in a claim, it does not go in the
   report, however obvious or well known it is.
2. Every factual sentence carries the citation marker of the claim it came from,
   exactly as written in the list — for example [2]. Put it at the end of the
   sentence or clause it supports. Do not invent a marker, do not use a number
   that is not in the list, and do not renumber anything.
3. Do not state anything stronger than the claim supports. If a claim says a
   figure is estimated, approximate, or from a single source, the sentence using
   it says so too.
4. Claims marked EXCLUDED are still worth reporting and you should use them where
   they are informative — but attribute them honestly, in-line, as what they are
   (a single uncorroborated source, a figure substituted from an aggregator, and
   so on). They do not count toward the band.
5. Claims marked ENTITY MISMATCH describe a DIFFERENT legal entity with a similar
   name. If you use one, say explicitly in the sentence that it refers to that
   other entity and not to the company under report. Never let one read as a fact
   about this company.
6. Write in continuous prose. No headings, no bullet lists, no tables, no
   confidence badge, no source list — those are added around your text
   afterwards. Bold a figure or a phrase where it genuinely carries the
   paragraph, sparingly.
7. Do not open with a restatement of the section title, and do not close with a
   summary of what you just said.

Aim for three to five substantial paragraphs. Fewer is correct when the claims
are thin — padding a thin section is the one failure this report cannot afford,
because the band already told the reader the evidence is thin and the prose must
not read as though it disagrees."""

PROMPT = """\
Company under report: {company} (Companies House number {number}).

{brief}

CONFIDENCE BAND FOR THIS SECTION — already computed, not yours to change:
  Band: {band}
  Why: {reason}
  Scored claims: {scored} of {total} total claims in this section.

Your prose must be consistent with that band. If the band is LOW or MEDIUM, the
writing should read as appropriately hedged; if it is HIGH, it should not hedge
what the evidence actually supports.

{conflicts}

{context}

CLAIMS — the complete evidence for this section:

{claims}

{rules}

Return ONLY a JSON object:
{{
  "prose": "the section's prose, as markdown paragraphs separated by blank lines",
  "gaps": ["what the evidence does not establish, one short sentence each"]
}}

"gaps" is for what an underwriter would want and the claims cannot tell them —
a figure that could not be verified, a comparison resting on one source, a
period with no coverage. State each as a specific absence, not a general
caveat. An empty list is correct only if the claims genuinely leave nothing
material unanswered, which is rare."""


class SectionProse(BaseModel):
    """The only thing synthesis returns. Everything else is rendered from data.

    A Pydantic model rather than hand-parsed JSON, per AGENTS.md — and narrow on
    purpose. There is no field here for a heading, a band, a source or a URL,
    so a model that tries to supply one has nowhere to put it.
    """

    prose: str = Field(default="")
    gaps: list[str] = Field(default_factory=list)


def format_claim(claim: EvidenceClaim, citations: SectionCitations) -> str:
    """One claim as the model sees it: marker, tier, text, and any flags.

    The URL is deliberately absent. The marker is the only handle the model has
    on a source, and it cannot mint a new one.
    """
    parts = [f"{citations.marker_for(claim.source_url)} ({claim.source_type.value})"]
    if claim.confidence.value != "direct":
        parts.append(f"[source support: {claim.confidence.value}]")
    if not claim.counts_toward_confidence:
        reason = claim.exclusion_reason or "not reliable enough to score"
        parts.append(f"[EXCLUDED from scoring — {reason}]")
    if claim.entity_warning:
        parts.append(f"[ENTITY MISMATCH — {claim.entity_warning}]")
    return f"- {' '.join(parts)} {claim.text}"


def format_claims(claims: Sequence[EvidenceClaim], citations: SectionCitations) -> str:
    if not claims:
        return (
            "(No claims were extracted for this section. Say so plainly in one or two "
            "sentences — that no usable evidence was retrieved for this section, and "
            "that this is an absence of evidence rather than an adverse finding. Write "
            "nothing else, and cite nothing.)"
        )
    return "\n".join(format_claim(claim, citations) for claim in claims)


def format_conflicts(conflicts: Sequence[Conflict]) -> str:
    """Conflicts as instructions, not as facts to reconcile.

    Phase 4.3's rule is that a disagreement surfaces rather than being resolved,
    so the prompt has to say that explicitly — a model given two contradicting
    figures will otherwise pick one, and picking one is the failure.
    """
    if not conflicts:
        return "CONFLICTS: none detected between the claims in this section."
    lines = [
        "CONFLICTS DETECTED — report both sides, never pick a winner and never "
        "quietly drop one side:"
    ]
    lines += [f"  - {conflict.severity.value.upper()}: {conflict.detail}" for conflict in conflicts]
    return "\n".join(lines)


def format_context(context: Sequence[str]) -> str:
    """A read-only digest of the other sections.

    Section 3 in particular benefits from knowing what Sections 1 and 2 found —
    the mock's most useful observation connects a customer-complaint theme to a
    regulatory finding. The digest is bands and headline facts only, and the
    prompt forbids citing from it, because a claim gathered for another section
    has its own citation numbering there and would resolve to the wrong source
    here.
    """
    if not context:
        return ""
    lines = [
        "CONTEXT FROM THE REST OF THE REPORT — for your awareness only. You may "
        "note a connection between sections where one genuinely exists, flagged as "
        "an observation rather than a documented link. You may NOT cite any of it: "
        "these facts are numbered differently in their own sections."
    ]
    lines += [f"  - {line}" for line in context]
    return "\n".join(lines)


def build_prompt(
    *,
    section: WebSection,
    company: str,
    number: str,
    assessment: ConfidenceAssessment,
    claims: Sequence[EvidenceClaim],
    citations: SectionCitations,
    conflicts: Sequence[Conflict] = (),
    context: Sequence[str] = (),
) -> str:
    return PROMPT.format(
        company=company,
        number=number,
        brief=_BRIEFS[section],
        band=assessment.band.value.upper(),
        reason=assessment.reason.value,
        scored=assessment.scored_claims,
        total=assessment.total_claims,
        conflicts=format_conflicts(conflicts),
        context=format_context(context),
        claims=format_claims(claims, citations),
        rules=_RULES,
    )


__all__ = [
    "MAX_SECTION_TOKENS",
    "PROMPT",
    "SYSTEM",
    "SectionProse",
    "build_prompt",
    "format_claim",
    "format_claims",
    "format_conflicts",
    "format_context",
]
