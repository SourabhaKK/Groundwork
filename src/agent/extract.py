"""Turning tagged documents into evidence claims (Phase 4.2 and 4.3).

This is the implementation behind `AgentDependencies.extractor`, the seam Phase 3
left open. The graph does not change: `extract_node` already merges what this
returns and `score_confidence()` already scores it.

Four decisions here are load-bearing, and each exists because of a specific
failure this project has already met or specifically refuses to allow.

**1. One model call per document, not per section.** Phase 4.2 requires
different extraction framing per source type, and source type is a property of a
document — a single section-level call would put a Companies House officer list
and a Trustpilot snippet in one prompt and let the model average the two
framings. Per-document also isolates failure (one unparseable response costs one
document, not a section) and, most importantly, makes the citation structural:
see 2.

**2. The model never supplies a URL.** `source_url`, `source_type`, `section`
and `origin` are all set here, from the `TaggedDocument` the call was made
about. The model returns only the fact, a verbatim quote, and how directly the
source supports it. AGENTS.md's hard boundary — never fabricate a citation — is
therefore not a prompt instruction that can erode but a shape the code cannot
express. A section-level prompt would have had to hand the model a menu of URLs
to pick from, which is exactly the mechanism by which a claim gets attached to
the wrong source.

**3. Register documents are extracted once, and the model routes each claim to a
section.** A register document has `section=None` because it serves all three
(incorporation date bears on business model, officer churn on quality signals).
Extracting it once per section would let one incorporation date become three
claims and pad every section's count — a band moving with no new evidence.
Instead the claim carries a closed three-way section choice made per fact.

**4. An entity-warned document can never produce a scoring claim, and that is
enforced in code.** TASKS.md 4.2 makes this a hard requirement rather than a
nuance: a Better Business Bureau complaint about "Monzo, Inc." rendered as a fact
about MONZO BANK LIMITED is a correctness failure. The prompt is told, and then
the result is corrected regardless of whether it listened — `_apply_entity_
warning` runs unconditionally. A prompt instruction is a request; only code makes
"must not silently absorb" structurally true.

**Granularity** (Phase 4.1) is carried into every prompt as the worked tie-
breaker from `docs/phase-4.1-claim-granularity-example.md`, plus one worked
example per source type. The model is never told how many claims to return —
that is the most direct route to the band-gaming the granularity rule exists to
prevent.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from collections.abc import Sequence

from pydantic import BaseModel, Field, ValidationError

from src.agent.claims import (
    ClaimConfidence,
    Conflict,
    ConflictSeverity,
    EvidenceClaim,
    ExtractionResult,
)
from src.data_sources.sources import EvidenceBundle, SourceType, TaggedDocument
from src.data_sources.web_gather import WebSection
from src.llm.json_parsing import json_object
from src.llm.router import AllProvidersFailedError, LLMRouter, LLMTier

logger = logging.getLogger(__name__)

# How much of a document the model is shown. Tavily snippets ran 559-2,404
# characters in the Phase 2 live trial, so this only ever bites on a register
# document with a long officer or filing list. Quote verification runs against
# the *full* content, so truncation can never turn a real quote into a rejected
# one — it only limits what the model can see to quote from.
MAX_DOCUMENT_CHARS = 8_000

# Concurrency bound across documents. A Monzo-sized run is ~19 documents; firing
# all of them at once is how a burst of 429s trips the breaker and demotes the
# whole run to the fallback model for something that is not an outage.
MAX_CONCURRENT_EXTRACTIONS = 6

# Above this, a single document is assumed to have been over-fragmented rather
# than to be unusually rich. Its claims are all kept — they still render in
# prose — but none of them score, with the reason stated on each.
#
# Added during implementation rather than specified up front, so worth stating
# plainly: the granularity rule's don't-split clause has no mechanical enforcer,
# and every count-based condition in `score_confidence` trusts it. This is a
# backstop in the safe direction only — it can withhold confidence, never
# manufacture it — chosen over dropping the surplus claims, which would delete
# evidence to defend a number.
MAX_CLAIMS_PER_DOCUMENT = 12

_MAX_OUTPUT_TOKENS = 1_500


# ---------------------------------------------------------------------------
# What the model is allowed to return
# ---------------------------------------------------------------------------


class _ProposedClaim(BaseModel):
    """One claim as the model proposes it — a fact, its evidence, its strength.

    Note what is absent: no URL, no source type, no citation of any kind. Those
    are known here already and are attached from the document. There is no field
    through which a fabricated source could arrive.
    """

    text: str = Field(min_length=1)
    quote: str = ""
    confidence: ClaimConfidence = ClaimConfidence.DIRECT
    section: WebSection | None = None  # only requested for register documents


class _ProposedClaims(BaseModel):
    claims: list[_ProposedClaim] = Field(default_factory=list)


class _ProposedConflict(BaseModel):
    """A disagreement, referencing claims by 1-based list position.

    Positions rather than claim ids: an id is a 12-character hash, and asking a
    model to copy hashes accurately is a needless failure mode. The positions are
    mapped back to real ids here, and anything out of range is discarded — the
    same discipline as never letting the model supply a URL.
    """

    claims: list[int] = Field(default_factory=list)
    severity: ConflictSeverity = ConflictSeverity.MINOR
    detail: str = ""


class _ProposedConflicts(BaseModel):
    conflicts: list[_ProposedConflict] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

_SYSTEM = (
    "You extract verifiable facts from documents for a UK commercial "
    "underwriting report. You return only JSON. You never state anything the "
    "document does not support, and you never guess at facts it does not "
    "contain. An empty claim list is a correct answer for a document that "
    "contains nothing usable."
)

# The tie-breaker, verbatim from docs/phase-4.1-claim-granularity-example.md.
# One string, used by every variant, so the rule cannot drift between prompts.
_GRANULARITY = """\
One claim is ONE atomic, independently-citable fact.

The test, when a fact sits near the boundary:
  Does the fact stand alone as something an underwriter could act on, or does it
  only make sense as a qualifier to another fact?
  Stands alone -> it is its own claim.
  Only makes sense as a qualifier -> fold it into the claim it qualifies.

Do not bundle distinct facts together just because they share a citation.
Do not split one fact into several by rephrasing it or quoting it twice."""

_REGISTRY_EXAMPLE = """\
Worked example (an FCA enforcement notice):
  - The penalty amount and its publication date -> one claim.
  - The period the failings covered -> a separate claim; recency is
    independently decisive for an underwriter.
  - Breaching a requirement the regulator imposed -> a separate claim; that is a
    governance fact, not the same fact as the control weakness.
  - A completed remediation programme -> a separate claim; it points the other
    way.
  - "The penalty would have been higher without a 30% early-settlement discount"
    -> NOT a separate claim. It has no standalone meaning; it explains how the
    penalty figure was reached, so it belongs inside that claim."""

_SELF_EXAMPLE = """\
Worked example (a company's own annual report):
  - Each distinct metric -> its own claim: revenue, profit before tax, deposits,
    total customers, monthly active users. Reach and engagement are different
    facts.
  - A growth percentage stated alongside its metric -> NOT a separate claim.
    "GBP 1.7bn" and "+39%" are one fact about one metric stated two ways, and
    counting them twice is the clearest form of over-fragmentation."""

_FRAMING: dict[str, str] = {
    "registry": """\
This document is from an independent public body — a register or a regulator.
It has no stake in what it says about the company.

- Extract what the document states outright. Figures, dates, statuses and
  identifiers should be reproduced exactly as written, not rounded or rephrased.
- Do not infer a consequence the document does not state. "Accounts were filed
  late" is in the document; "the company has cash-flow problems" is not.
- Absence recorded by a register is itself a fact, and worth a claim when the
  document states it (for example, no person registered as having significant
  control).""",
    "self": """\
This document is the company's own account of itself. It is primary and often
audited, but it is self-interested.

- Every claim must make the source of the statement visible in its own wording:
  write "the company reports X" or "the company states X", never a bare "X".
  A reader of the finished report must be able to see whose voice a fact is in
  without checking the citation.
- Extract figures and concrete commitments. Do not extract marketing language:
  "the UK's best current account" is a claim about nothing measurable.""",
    "press": """\
This document is third-party writing about the company — trade press, news, or a
general web page.

- Separate reported fact from the writer's commentary. A funding round, a
  named competitor, a product launch, a stated market position: those are facts
  the document reports. The writer's opinion about whether it was wise is not.
- Where the document attributes a fact to someone else ("according to the
  company", "sources said"), carry that attribution into the claim text.
- A speculative or forward-looking statement is at most `implied`, and often
  not a claim at all.""",
    "review": """\
This document is a review platform, a complaints page, or user-generated
opinion.

- Aggregate opinion is not a fact about the company, but the measurable shape of
  it is: a rating out of five, a number of reviews or complaints, a recurring
  named theme, the outcome of an adjudicated complaint.
- Extract those. Do not extract individual anecdotes as if they characterised
  the company, and do not aggregate sentiment yourself into a verdict.
- Where the document reports an adjudicated outcome (an ombudsman decision, a
  formally upheld complaint), that is a stronger fact than a rating and should
  be its own claim.""",
}

_PROMPT = """\
Company under report: {company} (UK company number {number}).

{framing}

{granularity}

{example}

{section_rule}

Document title: {title}
Document URL: {url}
Document content:
\"\"\"
{content}
\"\"\"

For each claim, return:
  "text"       - the fact, in one self-contained sentence. It must be
                 understandable without the document in front of you.
  "quote"      - a short span copied VERBATIM from the document content above
                 that supports the fact. Copy it exactly, character for
                 character. Do not paraphrase, correct or shorten it with an
                 ellipsis. If nothing in the document supports the fact
                 verbatim, do not make the claim.
  "confidence" - "direct" if the document states the fact outright, "implied" if
                 it is a reasonable reading rather than an explicit statement,
                 "uncertain" if the document is equivocal.{section_field}

Return only this JSON object, and nothing else:
{{"claims": [{{"text": "...", "quote": "...", "confidence": "direct"{section_json}}}]}}

If the document contains no usable fact about {company}, return {{"claims": []}}.
"""

_SECTION_RULE_INHERITED = """\
This document was gathered as evidence for the "{section}" section of the report.
Extract the facts in it that bear on that section."""

_SECTION_RULE_ASSIGNED = """\
This document serves the whole report, so each claim must say which section it
belongs to. Choose exactly one per claim:
  "business_model"        - what the company does and how it makes money
                            (activity, SIC codes, incorporation, structure).
  "competitive_landscape" - its market, named rivals, relative position.
  "reputation"            - conduct, complaints, regulatory action, how it is
                            regarded, and governance signals such as officer
                            turnover or filing punctuality."""

_ENTITY_WARNING_PREAMBLE = """\
WARNING - POSSIBLE ENTITY MISMATCH. This document appears to be about {warning},
which is NOT the company under report ({company}). It may be a foreign affiliate
or an unrelated company with a similar name.

Extract only what the document actually supports, and begin every claim's text
by naming the entity the document is really about, so no reader can mistake it
for a fact about {company}. Do not attribute anything here to {company}.

"""

_CONFLICT_SYSTEM = (
    "You compare extracted claims and report only genuine disagreements between "
    "them. You return only JSON. You never resolve a disagreement, rank the "
    "sources, or decide which is correct."
)

_CONFLICT_PROMPT = """\
Below are the claims extracted for the "{section}" section of a report on \
{company}. Each is numbered.

{claims}

Identify pairs or groups of claims that genuinely disagree with each other.

  MINOR conflict: the sources differ on phrasing or peripheral detail but agree
  on the core fact — two slightly different employee-count estimates, or two
  dates for the same event within a plausible range.

  MAJOR conflict: the sources contradict a core fact — one says the company was
  acquired or dissolved while the register shows it independently trading, or
  two sources give opposite signals on the same specific claim rather than
  merely differing in tone.

Rules:
- Differing in emphasis, tone or level of detail is NOT a conflict. Two claims
  about different things are NOT a conflict.
- Do not resolve anything, and do not say which claim is right. The report
  surfaces disagreement to its reader rather than settling it.
- If there are no genuine conflicts, that is the expected answer.

Return only this JSON object, and nothing else:
{{"conflicts": [{{"claims": [1, 2], "severity": "minor",
  "detail": "one sentence on what they disagree about"}}]}}
"""


# ---------------------------------------------------------------------------
# Quote verification
# ---------------------------------------------------------------------------

_PUNCTUATION = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "‚": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        "…": "...",
    }
)


def normalise(text: str) -> str:
    """Flatten a string to the form quotes are compared in.

    Deliberately forgiving about everything that is not the words themselves.
    Models reliably reproduce a sentence while normalising its typography — a
    curly apostrophe becomes straight, an en dash becomes a hyphen, wrapped
    lines lose their newline. Failing a claim over that would make the check
    measure transcription fidelity rather than grounding, and quietly strip real
    evidence out of the scoring math.

    It stays strict about word content, which is the thing being verified.
    """
    flattened = unicodedata.normalize("NFKC", text or "").translate(_PUNCTUATION)
    return re.sub(r"\s+", " ", flattened).strip().lower()


def quote_is_grounded(quote: str, content: str) -> bool:
    """Does this quote actually appear in the document it claims to come from?

    A substring test after normalisation, which is the honest bound of what can
    be checked cheaply: it catches a fabricated or materially reworded quote and
    does not pretend to catch a real quote that has been given a misleading
    interpretation. Phase 7.1a's rubric is where over-generalisation is judged;
    this is the mechanical half.
    """
    needle = normalise(quote)
    if len(needle) < 12:
        # Too short to be evidence of anything. "Monzo" appears in every document
        # about Monzo, so accepting it would make the check pass on any claim.
        return False
    return needle in normalise(content)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def _framing_key(source_type: SourceType) -> str:
    """Which of the four prompt variants a source type gets.

    Four, not five: `general_web` and `trade_press` differ in authority weight,
    which Phase 5.4 already applies at scoring time. As *documents* they are the
    same thing — third-party prose about the company — and giving them separate
    framings would encode a real difference in the wrong place, as an adjective
    the model can ignore.
    """
    if source_type is SourceType.OFFICIAL_REGISTRY:
        return "registry"
    if source_type is SourceType.OFFICIAL_SELF:
        return "self"
    if source_type is SourceType.REVIEW:
        return "review"
    return "press"


_EXAMPLES = {
    "registry": _REGISTRY_EXAMPLE,
    "self": _SELF_EXAMPLE,
    "press": _REGISTRY_EXAMPLE,
    "review": _REGISTRY_EXAMPLE,
}


def build_prompt(document: TaggedDocument, bundle: EvidenceBundle) -> str:
    """The full extraction prompt for one document."""
    key = _framing_key(document.source_type)
    assigns_section = document.section is None

    content = document.content or ""
    if len(content) > MAX_DOCUMENT_CHARS:
        content = content[:MAX_DOCUMENT_CHARS] + "\n[...document truncated]"

    prompt = _PROMPT.format(
        company=bundle.company_name,
        number=bundle.company_number,
        framing=_FRAMING[key],
        granularity=_GRANULARITY,
        example=_EXAMPLES[key],
        section_rule=(
            _SECTION_RULE_ASSIGNED
            if assigns_section
            else _SECTION_RULE_INHERITED.format(section=document.section.value.replace("_", " "))
        ),
        title=document.title or "(untitled)",
        url=document.url,
        content=content,
        section_field=(
            '\n  "section"    - one of "business_model", "competitive_landscape", "reputation".'
            if assigns_section
            else ""
        ),
        section_json=', "section": "business_model"' if assigns_section else "",
    )

    if document.entity_warning:
        prompt = (
            _ENTITY_WARNING_PREAMBLE.format(
                warning=document.entity_warning, company=bundle.company_name
            )
            + prompt
        )
    return prompt


def _apply_entity_warning(claim: EvidenceClaim, warning: str, company: str) -> EvidenceClaim:
    """Make a warned document's claim non-scoring and self-describing.

    Runs unconditionally, whatever the prompt got back. TASKS.md 4.2 allows
    either refusing the claims or marking them non-scoring with the mismatch
    stated in the claim text; this takes the second route because the warning is
    deliberately not a filter — whether a foreign affiliate matters is a
    judgement for the reader, and deleting the evidence makes that judgement
    impossible. Keeping it in prose while removing it from the math is precisely
    what `counts_toward_confidence` was added for.

    The prefix is written here rather than trusted to the model, because "must
    not silently absorb" is only true if a warned document *cannot* produce a
    scoring claim.
    """
    prefix = f'[Refers to "{warning}", not {company}]'
    text = claim.text if claim.text.startswith(prefix) else f"{prefix} {claim.text}"
    return claim.model_copy(
        update={
            "text": text,
            "counts_toward_confidence": False,
            "exclusion_reason": (
                f"source appears to describe {warning}, a different legal entity from {company}"
            ),
            "entity_warning": warning,
        }
    )


def _to_claims(
    proposals: Sequence[_ProposedClaim], document: TaggedDocument, bundle: EvidenceBundle
) -> list[EvidenceClaim]:
    """Attach provenance to what the model proposed, and score-gate it.

    Everything that makes a claim citable comes from `document`, never from the
    response. The only judgements taken from the model are the fact itself, its
    quote, how directly the source supports it, and — for a register document
    only — which section it belongs to.
    """
    claims: list[EvidenceClaim] = []

    for proposal in proposals:
        section = document.section or proposal.section
        if section is None:
            # A register document whose claim named no section. Dropping it is
            # right: it has nowhere to be scored or rendered, and guessing a
            # section would file a fact under a heading the model did not choose.
            logger.warning("extract %s: claim with no section, dropped", document.url)
            continue

        grounded = quote_is_grounded(proposal.quote, document.content)
        claim = EvidenceClaim(
            text=proposal.text.strip(),
            source_url=document.url,
            source_type=document.source_type,
            section=section,
            confidence=proposal.confidence,
            counts_toward_confidence=grounded,
            exclusion_reason=(
                None
                if grounded
                else "the supporting quote could not be found verbatim in the source text"
            ),
            origin=document.origin,
        )
        if not grounded:
            logger.info(
                "extract %s: quote not found in source, claim kept but not scored: %r",
                document.url,
                proposal.quote[:80],
            )
        if document.entity_warning:
            claim = _apply_entity_warning(claim, document.entity_warning, bundle.company_name)
        claims.append(claim)

    if len(claims) > MAX_CLAIMS_PER_DOCUMENT:
        logger.warning(
            "extract %s: %d claims from one document, above the granularity guard "
            "of %d — keeping them in prose but excluding all from scoring",
            document.url,
            len(claims),
            MAX_CLAIMS_PER_DOCUMENT,
        )
        reason = (
            f"one document produced {len(claims)} claims, above the granularity "
            f"guard of {MAX_CLAIMS_PER_DOCUMENT} — over-fragmentation suspected"
        )
        claims = [
            claim.model_copy(
                update={
                    "counts_toward_confidence": False,
                    "exclusion_reason": claim.exclusion_reason or reason,
                }
            )
            for claim in claims
        ]

    return claims


class ClaimExtractor:
    """Phase 4.2's implementation of the `Extractor` protocol.

    Stateless apart from the router, so one instance serves every run.
    """

    def __init__(self, router: LLMRouter, *, detect_conflicts: bool = True) -> None:
        self._router = router
        self._detect_conflicts = detect_conflicts

    async def __call__(
        self, evidence: EvidenceBundle, sections: Sequence[WebSection]
    ) -> ExtractionResult:
        """Extract claims for `sections`, then look for conflicts between them.

        `sections` is narrowed by the graph on Phase 3.5's second pass to only
        the sections that were re-searched.
        """
        documents = self._documents_for(evidence, sections)
        if not documents:
            return ExtractionResult()

        semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)

        async def one(document: TaggedDocument) -> list[EvidenceClaim]:
            async with semaphore:
                return await self._extract_document(document, evidence)

        results = await asyncio.gather(
            *(one(document) for document in documents), return_exceptions=True
        )

        claims: list[EvidenceClaim] = []
        for document, result in zip(documents, results, strict=True):
            if isinstance(result, AllProvidersFailedError):
                # Not degradable. Every provider being down is an outage, and an
                # outage rendered as a thin report is indistinguishable from an
                # honest report about a thinly-documented company — the one
                # failure this project refuses. `extract_node` turns this into a
                # PROVIDER_FAILURE halt.
                raise result
            if isinstance(result, BaseException):
                logger.warning(
                    "extract %s: failed (%s: %s), continuing without it",
                    document.url,
                    type(result).__name__,
                    result,
                )
                continue
            claims.extend(result)

        conflicts = await self._find_conflicts(claims, evidence, sections)
        logger.info(
            "extract %s: %d claim(s) from %d document(s), %d scored, %d conflict(s)",
            evidence.company_number,
            len(claims),
            len(documents),
            sum(1 for claim in claims if claim.counts_toward_confidence),
            len(conflicts),
        )
        return ExtractionResult(claims=claims, conflicts=conflicts)

    def _documents_for(
        self, evidence: EvidenceBundle, sections: Sequence[WebSection]
    ) -> list[TaggedDocument]:
        """Which documents this pass extracts from.

        Web documents for the requested sections, plus the register documents —
        but the register only on a full pass. On Phase 3.5's second pass, which
        requests one or two re-searched sections, the register has already been
        extracted and its claims are in state; re-extracting would spend tokens
        re-deriving claims `merge_claims` then discards as duplicates.
        """
        wanted = set(sections)
        full_pass = wanted == set(WebSection)
        return [
            document
            for document in evidence.documents
            if (document.section in wanted) or (document.section is None and full_pass)
        ]

    async def _extract_document(
        self, document: TaggedDocument, evidence: EvidenceBundle
    ) -> list[EvidenceClaim]:
        if not (document.content or "").strip():
            return []

        response = await self._router.complete(
            build_prompt(document, evidence),
            tier=LLMTier.EXTRACTION,
            system=_SYSTEM,
            max_tokens=_MAX_OUTPUT_TOKENS,
        )

        payload = json_object(response.text)
        if payload is None:
            logger.warning("extract %s: no JSON object in response", document.url)
            return []
        try:
            proposed = _ProposedClaims.model_validate_json(payload)
        except ValidationError as exc:
            logger.warning("extract %s: unusable JSON (%s)", document.url, exc.error_count())
            return []

        return _to_claims(proposed.claims, document, evidence)

    # -----------------------------------------------------------------
    # Phase 4.3 — conflict detection
    # -----------------------------------------------------------------

    async def _find_conflicts(
        self,
        claims: Sequence[EvidenceClaim],
        evidence: EvidenceBundle,
        sections: Sequence[WebSection],
    ) -> list[Conflict]:
        """One comparison pass per section, over that section's claims.

        Per section because a conflict is only meaningful between claims about
        the same thing, and because Phase 5.4 scores per section — a conflict
        spanning two sections has no band to affect.

        Excluded claims are included in the comparison. They still render in
        prose, so a disagreement involving one is worth showing the reader; and
        `score_confidence` already declines to let such a conflict move a band,
        because it only counts conflicts where every referenced claim scores.
        """
        if not self._detect_conflicts:
            return []

        targets = [
            section for section in sections if len([c for c in claims if c.section is section]) >= 2
        ]
        if not targets:
            return []

        results = await asyncio.gather(
            *(self._conflicts_in(section, claims, evidence) for section in targets),
            return_exceptions=True,
        )

        conflicts: list[Conflict] = []
        for section, result in zip(targets, results, strict=True):
            if isinstance(result, AllProvidersFailedError):
                raise result
            if isinstance(result, BaseException):
                # Degrades, deliberately. A missed conflict leaves a section
                # scored as if its evidence agreed, which is the optimistic
                # direction — but the alternative is failing a whole report over
                # a comparison pass, and the claims themselves are unaffected.
                logger.warning(
                    "conflict detection failed for %s (%s), continuing",
                    section.value,
                    type(result).__name__,
                )
                continue
            conflicts.extend(result)
        return conflicts

    async def _conflicts_in(
        self,
        section: WebSection,
        claims: Sequence[EvidenceClaim],
        evidence: EvidenceBundle,
    ) -> list[Conflict]:
        in_section = [claim for claim in claims if claim.section is section]
        listing = "\n".join(
            f"{index}. {claim.text}  [source: {claim.source_url}]"
            for index, claim in enumerate(in_section, start=1)
        )

        response = await self._router.complete(
            _CONFLICT_PROMPT.format(
                section=section.value.replace("_", " "),
                company=evidence.company_name,
                claims=listing,
            ),
            tier=LLMTier.EXTRACTION,
            system=_CONFLICT_SYSTEM,
            max_tokens=_MAX_OUTPUT_TOKENS,
        )

        payload = json_object(response.text)
        if payload is None:
            return []
        try:
            proposed = _ProposedConflicts.model_validate_json(payload)
        except ValidationError:
            logger.warning("conflict detection %s: unusable JSON", section.value)
            return []

        conflicts: list[Conflict] = []
        for item in proposed.conflicts:
            # Positions are mapped to real ids here, and an out-of-range position
            # is discarded rather than clamped. A conflict pointing at a claim
            # that does not exist is not a conflict, and inventing which claim
            # was meant would attach a disagreement to evidence that never
            # carried it.
            ids = [
                in_section[position - 1].id
                for position in dict.fromkeys(item.claims)
                if 1 <= position <= len(in_section)
            ]
            if len(ids) < 2:
                logger.info(
                    "conflict detection %s: dropped a conflict citing %r",
                    section.value,
                    item.claims,
                )
                continue
            conflicts.append(
                Conflict(
                    severity=item.severity,
                    section=section,
                    claim_ids=ids,
                    detail=item.detail.strip() or "sources disagree",
                )
            )
        return conflicts


__all__ = [
    "MAX_CLAIMS_PER_DOCUMENT",
    "MAX_CONCURRENT_EXTRACTIONS",
    "MAX_DOCUMENT_CHARS",
    "ClaimExtractor",
    "build_prompt",
    "normalise",
    "quote_is_grounded",
]
