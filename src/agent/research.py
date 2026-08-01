"""Refined queries for Phase 3.5's targeted re-search.

**Why an LLM generates these, when Phase 2.2 refused to let one generate the
first-pass queries.** That refusal stands and is not being reversed: generating
the initial queries would make the gather layer non-deterministic exactly where
it must not be, and a badly generated query would be indistinguishable from a
company with no press coverage — this project's worst failure mode, reintroduced
at the source. Phase 2.2 explicitly relocated adaptive querying to here, because
at this point the refinement runs against a *measured* extraction gap rather
than a guess made before any evidence exists. The first pass stays deterministic
and templated; only the retry adapts.

**The correctness risk is a paraphrase.** An LLM asked to "refine" a query will
happily return the same query with different adjectives, which spends a search
credit to retrieve the same documents and produces a re-search that flatters
itself. So the generated query is validated mechanically before any credit is
spent, and a query that fails validation is replaced by a fixed alternate
template rather than used. The same fallback covers an LLM call that fails
outright, so re-search degrades rather than disappearing — the partial-tolerance
contract the gather layer already follows.
"""

from __future__ import annotations

import json
import logging
import re
from enum import StrEnum

from pydantic import BaseModel, Field, ValidationError

from src.data_sources.companies_house import CompanyProfile
from src.data_sources.sources import distinctive_tokens
from src.data_sources.web_gather import WebSection, build_queries, trading_name
from src.llm.router import LLMRouter, LLMTier

logger = logging.getLogger(__name__)

MAX_QUERY_LENGTH = 200

# Above this token overlap with the original, a "refinement" is a paraphrase.
# 0.8 is deliberately permissive: the company name, "UK", and the section's
# subject noun are all expected to recur, and they alone can account for much of
# the overlap on a short query. The threshold is there to catch a query that is
# *only* those tokens rearranged.
MAX_SIMILARITY = 0.8

# Deliberately different angles, not reworded versions of `build_queries`: a
# different document type (filings, sector analysis, adjudications) rather than
# a different phrasing of the same request. These are the floor on refinement
# quality — the LLM has to beat them to be used.
_FALLBACK_TEMPLATES: dict[WebSection, str] = {
    WebSection.BUSINESS_MODEL: (
        "{short} UK revenue streams, pricing and customer segments "
        "in its annual report or company filings"
    ),
    WebSection.COMPETITIVE_LANDSCAPE: (
        "UK market share and named rivals to {short} — sector analysis "
        "comparing it against similar companies"
    ),
    WebSection.REPUTATION: (
        "{short} UK Trustpilot rating, Financial Ombudsman decisions "
        "and outcomes of customer complaints"
    ),
}

_SYSTEM = (
    "You write web search queries for an underwriting research tool. "
    "You return only JSON. You never invent facts about the company."
)

_PROMPT = """\
A first search for the "{section}" section of a report on {registered} \
(commonly written {short}), a company on the UK register, returned too little \
usable evidence.

Original query:
  {original}

Sources it already returned (do not simply target these again):
{seen}

What extraction could not establish:
  {gap}

Write ONE different search query that targets that specific gap. Requirements:
- It must name {short}, and it must be anchored to the UK.
- It must seek a DIFFERENT kind of source or angle than the original — a
  different document type, a different vocabulary a different publisher would
  use — not the original reworded.
- Under {limit} characters. No quotes, no search operators.

Return only this JSON object:
{{"query": "...", "targets_gap": "one clause on what it goes after",
  "differs_because": "one clause on how it differs from the original"}}
"""


class RefinementSource(StrEnum):
    """Where the query actually came from — recorded in the 3.4 decision log.

    A run where every refinement silently fell back to a template is a run where
    the adaptive step did nothing, and that must be visible in the log rather
    than inferred from search results.
    """

    MODEL = "model"
    FALLBACK_TEMPLATE = "fallback_template"


class RefinedQuery(BaseModel):
    """The model's proposal, before validation."""

    query: str = Field(min_length=1)
    targets_gap: str = ""
    differs_because: str = ""


class QueryRefinement(BaseModel):
    """What 3.5 will actually search with, and the audit trail for why."""

    section: WebSection
    original: str
    query: str
    source: RefinementSource
    targets_gap: str = ""
    differs_because: str = ""
    rejected_query: str | None = None  # what the model proposed, if unusable
    rejection_reason: str | None = None

    def describe(self) -> str:
        base = f"{self.section.value}: {self.query!r} (via {self.source.value})"
        if self.rejection_reason:
            base += f"; model proposal rejected — {self.rejection_reason}"
        return base


_ANCHORED_TO_UK = re.compile(r"\b(uk|u\.k\.|united kingdom|britain|british)\b", re.IGNORECASE)


def _tokens(text: str) -> set[str]:
    """Word tokens, dropping the very short ones.

    The length filter keeps articles and prepositions from inflating the
    similarity score between two queries that share nothing but grammar. It also
    means this must never be used to test for "uk" — see `validate_refinement`.
    """
    return {token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2}


def similarity(left: str, right: str) -> float:
    """Jaccard overlap on word tokens.

    Chosen over an embedding or edit distance because it is deterministic, has
    no dependency, and is legible in a test failure — a reviewer can compute it
    by hand from the two queries and see why a refinement was rejected.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _mentions_company(query: str, short: str) -> bool:
    """The name's *distinctive* tokens must survive into the refined query.

    A refinement that drops the company name is a query about the sector, and
    its results would be attributed to this company in the report — a grounding
    failure, not a relevance one.

    Distinctive rather than every token, using the same list that stops "The
    Bank Group" claiming bank.com as its own domain. Requiring every token would
    reject "Monzo UK ombudsman decisions" for a company registered as MONZO BANK
    LIMITED — a good refinement, thrown away because it omitted a word that
    identifies nobody. Every such rejection silently downgrades the adaptive
    step to a fixed template.
    """
    required = distinctive_tokens(short)
    return bool(required) and required <= _tokens(query)


def validate_refinement(
    proposal: str, *, original: str, short: str
) -> tuple[str | None, str | None]:
    """Return `(usable_query, rejection_reason)` — exactly one is None.

    A repaired query is returned rather than rejected where repair is
    unambiguous (a missing UK anchor), and rejected where it is not (a missing
    company name, or a paraphrase).
    """
    query = " ".join(proposal.split())

    if not query:
        return None, "empty query"

    if len(query) > MAX_QUERY_LENGTH:
        return None, f"query too long ({len(query)} > {MAX_QUERY_LENGTH})"

    if not _mentions_company(query, short):
        return None, f"does not name the company ({short!r})"

    # Unambiguous repair: the anchor exists to keep a brand-name query on the UK
    # legal entity, after a live trial returned a US affiliate's complaints page
    # for a query about the UK bank. Appending it changes nothing else.
    #
    # Not checked via `_tokens`, which drops anything under three characters and
    # so can never contain "uk" — that made this branch fire unconditionally and
    # append a second " UK" to queries that already had one.
    if not _ANCHORED_TO_UK.search(query):
        query = f"{query} UK"

    overlap = similarity(query, original)
    if overlap > MAX_SIMILARITY:
        return None, f"too similar to the original query (overlap {overlap:.2f})"

    return query, None


def fallback_query(section: WebSection, profile: CompanyProfile) -> str:
    return _FALLBACK_TEMPLATES[section].format(short=trading_name(profile.name))


def _parse(raw: str) -> RefinedQuery | None:
    """Validate the model's JSON with Pydantic, never by hand.

    Only fenced-code stripping happens here; the fields themselves are parsed by
    `model_validate_json`, per AGENTS.md's rule that no LLM response is picked
    apart field by field in application code.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()

    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return RefinedQuery.model_validate_json(text[start : end + 1])
    except ValidationError:
        return None


async def refine_query(
    *,
    section: WebSection,
    profile: CompanyProfile,
    gap: str,
    seen_urls: list[str],
    router: LLMRouter,
) -> QueryRefinement:
    """Propose a targeted follow-up query for one under-evidenced section.

    Never raises. Every failure path — provider outage, unparseable JSON, a
    paraphrase — lands on the section's fallback template, because a re-search
    that vanishes when the model misbehaves would make the adaptive step
    silently conditional on the LLM rather than on the evidence.
    """
    short = trading_name(profile.name)
    original = build_queries(profile)[section]
    fallback = fallback_query(section, profile)

    def as_fallback(rejected: str | None, reason: str) -> QueryRefinement:
        logger.info("3.5 refinement fell back for %s: %s", section.value, reason)
        return QueryRefinement(
            section=section,
            original=original,
            query=fallback,
            source=RefinementSource.FALLBACK_TEMPLATE,
            targets_gap=gap,
            differs_because="fixed alternate template targeting a different source type",
            rejected_query=rejected,
            rejection_reason=reason,
        )

    listed = "\n".join(f"  - {url}" for url in seen_urls[:8]) or "  (none)"
    prompt = _PROMPT.format(
        section=section.value.replace("_", " "),
        registered=profile.name,
        short=short,
        original=original,
        seen=listed,
        gap=gap,
        limit=MAX_QUERY_LENGTH,
    )

    try:
        response = await router.complete(
            prompt, tier=LLMTier.EXTRACTION, system=_SYSTEM, max_tokens=400
        )
    except Exception as exc:  # noqa: BLE001 - any provider failure degrades, never propagates
        return as_fallback(None, f"llm unavailable ({type(exc).__name__})")

    proposal = _parse(response.text)
    if proposal is None:
        return as_fallback(None, "model returned no usable JSON")

    query, reason = validate_refinement(proposal.query, original=original, short=short)
    if query is None:
        return as_fallback(proposal.query, reason or "rejected")

    return QueryRefinement(
        section=section,
        original=original,
        query=query,
        source=RefinementSource.MODEL,
        targets_gap=proposal.targets_gap or gap,
        differs_because=proposal.differs_because,
    )


def as_json(refinement: QueryRefinement) -> str:
    """Compact form for the decision log's `detail` field."""
    return json.dumps(refinement.model_dump(mode="json"), sort_keys=True)


__all__ = [
    "MAX_QUERY_LENGTH",
    "MAX_SIMILARITY",
    "QueryRefinement",
    "RefinedQuery",
    "RefinementSource",
    "fallback_query",
    "refine_query",
    "similarity",
    "validate_refinement",
]
