"""Resolving a typed company name to one specific legal entity.

This is the front door of the whole system, and the place where a silent wrong
answer is most expensive. If an underwriter is shown the wrong "Smith
Construction Ltd", every downstream claim is still correctly cited to its
source — the traceability guarantee holds perfectly — while the entire report
is about the wrong company. So the bar for resolving automatically is
deliberately high, and the default is to show a human the candidates with
enough detail to choose.

How it works:

1. Search the register by name (thin records — the search endpoint does not
   return SIC codes).
2. Pre-score on what search *does* give us (name and status) and shortlist the
   top few, plus a bounded few the register flagged as former-name matches,
   which score nothing on current-name similarity and would otherwise never be
   looked at.
3. Hydrate the shortlist with concurrent profile lookups, because ranking on
   "SIC relevance" and showing an underwriter a distinguishing address both
   require the full record.
4. Identify the single leading candidate, then decide whether it can be
   accepted silently. Auto-resolve only on an exact normalised-name match that
   is unique among active candidates, is a going concern, and is not a dormant
   shell. Anything short of that returns NEEDS_CONFIRMATION with the reasons
   attached — the company is still shown, the automation is what's withheld.

On that last gate: "active" on the register means "not dissolved", not
"trading". A dormant shell whose name matches exactly would otherwise clear a
name-and-status check untouched, because the SIC penalty in the ranking only
ever sorts candidates *against each other* and is never consulted when one wins
outright. That would let the single worst outcome — an entire report built
about a shell company — arrive through the one door the ranking doesn't cover.

Reasons are a list, never a single value. A dissolved company found under a
former name is both of those things at once, and reporting whichever was
checked first would drop the other on precisely the cases that deserve the most
care.

When nothing matches at all, one relaxed retry drops legal suffixes and filler
tokens before giving up. Former-name matching is the last resort. Discovery of
it is cheap — the register flags a former-name hit in the search response's
`snippet` — but confirmation is not allowed to be: `snippet` is an
undocumented relevance artifact, so it only ever decides who gets hydrated,
and the match is confirmed against `previous_company_names` on the full
profile before anything is surfaced. Discovery is opportunistic; the fact is
not.

What this still cannot do: resolve a trading name that was never a registered
one. Confirmed against the live register — "Mondo" was Monzo's brand, never
its legal name, and returns nothing for Monzo at any rank. There is no record
to match, so no matching logic reaches it. Disclosed in the README rather than
left to be discovered.

Step 3 is all-or-nothing: if any hydration call fails, the whole resolution
fails. The uniqueness rule in step 4 is only a guarantee if every candidate it
considered was actually seen — ranking on the survivors would quietly turn
"this is the only exact active match" into "this is the only one we managed to
load", which is a different and much weaker claim.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from src.data_sources.companies_house import (
    LEGAL_SUFFIXES,
    CompaniesHouseAuthError,
    CompaniesHouseClient,
    CompaniesHouseUnavailable,
    CompanyProfile,
    CompanySearchResult,
    is_dissolved,
    looks_like_company_number,
    normalise_company_number,
)

logger = logging.getLogger(__name__)

# The register's own relevance order decides what we ever see, and we only get
# to re-rank what it hands back. Live testing showed three former-name matches
# from unrelated companies ranking above MONZO BANK LIMITED for q="Monzo" — at
# 20, enough such noise pushes the correct current entity past the window and
# it disappears before our own ranking gets a vote, with no error raised and a
# candidate list that simply lacks the right answer. 100 is the register's
# per-page maximum and costs the same single request.
SEARCH_LIMIT = 100

# How many candidates we pay a profile fetch for. Distinct from SEARCH_LIMIT on
# purpose: fetching wide is nearly free, hydrating wide is not.
SHORTLIST_SIZE = 5

# Extra profile fetches reserved for candidates the register flagged as
# former-name matches. They score ~0 on current-name similarity, so without a
# reserved slot they are never hydrated — which made former-name resolution
# reachable only for companies that already ranked well on their current name,
# i.e. exactly the case that didn't need it. Bounded so the worst case is
# SHORTLIST_SIZE + this, not "hydrate everything the search returned".
FORMER_NAME_SLOTS = 3

# SIC codes that mean the company is registered but not trading. A dormant
# shell frequently outranks the real trading entity on name similarity alone,
# which is exactly the mistake this ranking exists to prevent.
SHELL_SIC_CODES = frozenset({"99999", "74990"})

# Tokens people append that the register generally doesn't carry. Stripped only
# on the one relaxed retry, never on the first search — dropping them up front
# would throw away signal that legitimately distinguishes two companies.
_FILLER_TOKENS = frozenset({"uk", "group", "holdings", "the"})

# A query longer than this is not a company name. The register's own limit on a
# registered name is 160 characters.
MAX_QUERY_LENGTH = 160


class ResolutionKind(StrEnum):
    RESOLVED = "resolved"  # one company, confidently
    NEEDS_CONFIRMATION = "needs_confirmation"  # one company, but don't assume
    AMBIGUOUS = "ambiguous"  # several plausible; a human must choose
    NOT_FOUND = "not_found"  # the register returned nothing
    INVALID = "invalid"  # the input could not be a company at all


class ConfirmationReason(StrEnum):
    """Why a single, otherwise-clean match still needs a human to say yes.

    A list of these, not one: a company can be both a previous-name match and
    dissolved at the same time, and reporting only the first found would drop
    the other on exactly the cases that most deserve a careful look.
    """

    DORMANT_SIC = "dormant_sic"  # registered, but declares no trading activity
    DISSOLVED = "dissolved"  # no longer on the register as a going concern
    NOT_ACTIVE = "not_active"  # e.g. administration — neither active nor dead
    PREVIOUS_NAME = "previous_name"  # matched a name the company used to have
    RELAXED_MATCH = "relaxed_match"  # found only after we altered the query


class Candidate(BaseModel):
    """One ranked candidate, carrying the detail that distinguishes it.

    The profile is included whole rather than flattened: the disambiguation UI
    (Phase 6.2) needs status, SIC codes, incorporation date and address, and
    picking a subset here would mean revisiting this model every time that UI
    wants one more field.
    """

    profile: CompanyProfile
    score: float
    match: str  # why it ranked where it did, in one word — for the 3.4 log
    matched_previous_name: str | None = None


class Resolution(BaseModel):
    """Outcome of resolving one query."""

    kind: ResolutionKind
    query: str
    profile: CompanyProfile | None = None
    candidates: list[Candidate] = Field(default_factory=list)
    reasons: list[ConfirmationReason] = Field(default_factory=list)
    detail: str | None = None  # human-readable, for INVALID and NOT_FOUND


def normalise_name(raw: str) -> str:
    """Lowercase, strip punctuation and trailing legal-form suffixes."""
    text = re.sub(r"[^\w\s]", " ", (raw or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed:
        changed = False
        for suffix in LEGAL_SUFFIXES:
            if text.endswith(" " + suffix):
                text = text[: -(len(suffix) + 1)].strip()
                changed = True
    return text


def _name_score(query: str, name: str) -> tuple[float, str]:
    q, n = normalise_name(query), normalise_name(name)
    if not q or not n:
        return 0.0, "none"
    if q == n:
        return 3.0, "exact"
    if n.startswith(q):
        return 1.5, "prefix"
    if q in n:
        return 0.5, "contains"
    return 0.0, "none"


def _status_score(status: str | None) -> float:
    if status == "active":
        return 2.0
    if is_dissolved(status):
        return -2.0
    return 0.0


def is_shell_sic(sic_codes: list[str]) -> bool:
    """True if every code the company declares says it isn't really trading.

    Used twice, for different jobs: to penalise a shell in the ranking, and to
    block one from auto-resolving at all.
    """
    return bool(sic_codes) and all(code in SHELL_SIC_CODES for code in sic_codes)


def _sic_score(sic_codes: list[str]) -> float:
    """SIC relevance, defined concretely.

    The user gives us no sector to be "relevant" to, so relevance here means
    penalising codes that say the company isn't really trading, rather than an
    undefined notion of topical fit.
    """
    if not sic_codes:
        return 0.0
    if is_shell_sic(sic_codes):
        return -1.5
    return 0.5


def _prescore(query: str, result: CompanySearchResult) -> float:
    """Cheap ranking on search fields only, to pick who is worth hydrating.

    Scored against the candidate's *current* registered name, never against
    whatever the register matched on. This is the property that stops another
    company's former name outranking the company the user actually named:
    a former-name-only hit scores zero here and competes for a reserved slot
    instead (see `_former_name_candidates`), not for a general one.

    Stated explicitly and pinned by test rather than left to emerge from the
    fact that `_name_score` happens to see only `result.name` — that is true
    today by construction, and would silently reverse if this function ever
    learned about `snippet`.
    """
    return _name_score(query, result.name)[0] + _status_score(result.status)


def _matched_previous_name(query: str, profile: CompanyProfile) -> str | None:
    """The former name this query matches exactly, if any.

    Exact-on-normalised only. A looser test here would resurrect the fuzzy
    matching rejected for the current-name path, in the one place where being
    wrong is hardest to notice: the company would look plausibly related.
    """
    q = normalise_name(query)
    for previous in profile.previous_names:
        if q and normalise_name(previous.name) == q:
            return previous.name
    return None


def _looks_like_former_name_match(query: str, result: CompanySearchResult) -> bool:
    """Did the register match this candidate on something other than its title?

    Deliberately loose — `snippet` holds the whole former name ("MONDO
    MORTGAGES LIMITED") while the query may be one word of it, so requiring
    equality here would discard the signal. Looseness is safe precisely
    because this only decides who gets hydrated; the former-name match itself
    is confirmed exactly, against `previous_company_names`, afterwards. The
    cost of being wrong here is one wasted profile fetch, not a wrong answer.
    """
    if not result.snippet:
        return False
    return _name_score(query, result.snippet)[0] > 0


def _select_shortlist(query: str, results: list[CompanySearchResult]) -> list[CompanySearchResult]:
    """Who is worth a profile fetch: the best current-name matches, plus a
    bounded number of former-name candidates that would otherwise never rank.

    A relevance floor runs first. The register fuzzy-matches almost any input,
    so "Nonexistent Widgets Xyzzy" comes back with AJAX WIDGETS LIMITED and
    XYZZY LIMITED — real companies, zero connection to the query. Passing those
    through spent five profile fetches to present nonsense as a plausible
    candidate list, which is worse than saying nothing was found.

    Former-name candidates are exempt, and the exemption is the whole point:
    they score zero on current-name similarity *by design*, because the name
    that matched is one the company no longer has. Applying the floor to them
    would delete Fix 2's mechanism entirely — silently, since the result would
    still look like a reasonable "not found".
    """
    relevant = [r for r in results if _name_score(query, r.name)[1] != "none"]
    by_score = sorted(relevant, key=lambda r: -_prescore(query, r))
    shortlist = by_score[:SHORTLIST_SIZE]
    taken = {r.number for r in shortlist}

    # Drawn from the unfiltered results, not from `relevant` — that is the
    # exemption. Pinned by test_former_name_candidate_survives_relevance_floor.
    reserved = [
        r for r in results if r.number not in taken and _looks_like_former_name_match(query, r)
    ][:FORMER_NAME_SLOTS]

    if reserved:
        logger.info(
            "resolve %r: reserving %d hydration slot(s) for former-name candidates %s",
            query,
            len(reserved),
            [r.number for r in reserved],
        )
    return shortlist + reserved


def _score(query: str, profile: CompanyProfile) -> Candidate:
    name_score, match = _name_score(query, profile.name)
    total = name_score + _status_score(profile.status) + _sic_score(profile.sic_codes)
    return Candidate(
        profile=profile,
        score=total,
        match=match,
        matched_previous_name=_matched_previous_name(query, profile),
    )


def _confirmation_reasons(
    profile: CompanyProfile,
    matched_previous_name: str | None = None,
    *,
    relaxed: bool = False,
) -> list[ConfirmationReason]:
    """Every reason this company shouldn't just be accepted. Order is stable.

    Accumulated rather than returned on first hit: a dissolved company found
    under a former name is both things at once, and an underwriter told only
    one of those is being told the less alarming half at random.
    """
    reasons: list[ConfirmationReason] = []
    if relaxed:
        reasons.append(ConfirmationReason.RELAXED_MATCH)
    if matched_previous_name:
        reasons.append(ConfirmationReason.PREVIOUS_NAME)
    if is_dissolved(profile.status):
        reasons.append(ConfirmationReason.DISSOLVED)
    elif profile.status != "active":
        # Administration, voluntary arrangement, and anything the register adds
        # later. Not dissolved — saying so would overstate it — but not a going
        # concern either, and an underwriter needs to see that before a report
        # is written as though nothing were wrong.
        reasons.append(ConfirmationReason.NOT_ACTIVE)
    if is_shell_sic(profile.sic_codes):
        reasons.append(ConfirmationReason.DORMANT_SIC)
    return reasons


def _validate_query(text: str) -> str | None:
    """Why this input can't be a company, or None if it might be.

    Checked before any network call: a request we know is meaningless should
    not spend a rate-limited call to find that out.
    """
    if not text:
        return "Enter a company name or registration number."
    if len(text) > MAX_QUERY_LENGTH:
        return f"That is longer than a registered company name can be ({MAX_QUERY_LENGTH} chars)."
    if not any(character.isalnum() for character in text):
        return "That contains no letters or numbers, so it cannot be a company name."
    if len(text) < 2:
        return "A single character is too short to identify a company."
    return None


def _relax(text: str) -> str | None:
    """The query minus legal-form suffixes and filler, or None if unchanged.

    Used once, only after an empty search. Someone typing "Acme Group UK Ltd"
    may be describing a company registered simply as "ACME LIMITED" — but those
    tokens can equally be the thing that distinguishes two real companies, so
    they are only dropped once the precise query has already found nothing.
    """
    stripped = normalise_name(text)
    relaxed = " ".join(token for token in stripped.split() if token not in _FILLER_TOKENS)
    if not relaxed or relaxed == text.strip().lower():
        return None  # nothing was actually removed — a second search would repeat the first
    return relaxed


def _rank(candidates: list[Candidate]) -> list[Candidate]:
    """Highest score first. Ties break deterministically, never by luck."""
    return sorted(
        candidates,
        key=lambda c: (
            -c.score,
            c.profile.incorporated_on or date.max,
            c.profile.number,
        ),
    )


async def _hydrate(client: CompaniesHouseClient, numbers: list[str]) -> list[CompanyProfile]:
    """Fetch every shortlisted profile, or fail the whole resolution.

    All-or-nothing on purpose — see this module's docstring. `return_exceptions`
    is used so that one failure doesn't leave the other in-flight requests
    orphaned mid-gather.
    """
    results = await asyncio.gather(
        *(client.get_company_profile(number) for number in numbers),
        return_exceptions=True,
    )

    profiles: list[CompanyProfile] = []
    for number, result in zip(numbers, results, strict=True):
        if isinstance(result, CompaniesHouseAuthError):
            # Not remapped: a rejected key is our misconfiguration, and dressing
            # it up as "the register is down" would point debugging at the wrong
            # system entirely.
            raise result
        if isinstance(result, BaseException):
            logger.warning("hydration failed for %s: %s", number, result)
            raise CompaniesHouseUnavailable(
                f"Could not load every candidate profile (failed on {number}); "
                "refusing to rank on a partial shortlist"
            ) from result
        profiles.append(result)
    return profiles


def _accept(query: str, candidate: Candidate, *, relaxed: bool = False) -> Resolution:
    """Turn the single leading candidate into a verdict.

    One place decides between "resolved" and "needs confirmation", so the
    number-lookup path and the name-search path can never drift into applying
    different standards to the same company.
    """
    reasons = _confirmation_reasons(
        candidate.profile, candidate.matched_previous_name, relaxed=relaxed
    )
    if not reasons:
        logger.info(
            "resolve %r: auto-resolved to %s (%s)",
            query,
            candidate.profile.name,
            candidate.profile.number,
        )
        return Resolution(kind=ResolutionKind.RESOLVED, query=query, profile=candidate.profile)

    logger.info(
        "resolve %r: withholding auto-resolve for %s (%s) — %s",
        query,
        candidate.profile.name,
        candidate.profile.number,
        [r.value for r in reasons],
    )
    return Resolution(
        kind=ResolutionKind.NEEDS_CONFIRMATION,
        query=query,
        profile=candidate.profile,
        candidates=[candidate],
        reasons=reasons,
    )


async def resolve_company(query: str, client: CompaniesHouseClient) -> Resolution:
    """Resolve a user's input to one company, to a set of choices, or to a
    specific, statable reason why neither was possible."""
    text = (query or "").strip()

    problem = _validate_query(text)
    if problem:
        logger.info("resolve %r: rejected before search — %s", text, problem)
        return Resolution(kind=ResolutionKind.INVALID, query=text, detail=problem)

    # A company number is unambiguous by construction — no ranking needed. It
    # still goes through the same acceptance check: knowing exactly which
    # company you mean is not the same as it being safe to report on.
    if looks_like_company_number(text):
        number = normalise_company_number(text)
        logger.info("resolve %r: treating as company number %s", text, number)
        profile = await client.get_company_profile(number)
        return _accept(text, Candidate(profile=profile, score=0.0, match="number"))

    results = await client.search_companies(text, limit=SEARCH_LIMIT)

    # What the candidates are actually scored against. If the precise query
    # found nothing usable and a relaxed one did, ranking still has to compare
    # against the query that produced the results — otherwise every candidate
    # scores "no match" against words the register never had.
    search_text = text
    was_relaxed = False
    shortlist = _select_shortlist(search_text, results)

    # An empty shortlist covers both "the register returned nothing" and "it
    # returned things, none of which resemble the query" — the same outcome
    # from the user's side, so the same relaxed retry earns its keep on both.
    if not shortlist:
        relaxed = _relax(text)
        if relaxed:
            logger.info("resolve %r: nothing relevant, retrying relaxed as %r", text, relaxed)
            retried = await client.search_companies(relaxed, limit=SEARCH_LIMIT)
            reshortlisted = _select_shortlist(relaxed, retried)
            if reshortlisted:
                shortlist, search_text, was_relaxed = reshortlisted, relaxed, True

    if not shortlist:
        logger.info("resolve %r: no relevant candidates (register returned %d)", text, len(results))
        return Resolution(
            kind=ResolutionKind.NOT_FOUND,
            query=text,
            detail=(
                "The register has no company under that name. If the company "
                "trades under a name different from its registered one, try the "
                "registered legal name or the company number."
            ),
        )

    profiles = await _hydrate(client, [r.number for r in shortlist])
    ranked = _rank([_score(search_text, profile) for profile in profiles])

    exact = [c for c in ranked if c.match == "exact"]
    exact_active = [c for c in exact if c.profile.status == "active"]

    # An active exact match wins over a dead one of the same name — the common
    # case of an old dissolved "X Ltd" sitting alongside the current trading
    # "X Ltd" is not a real ambiguity, and asking about it every time would
    # train an underwriter to click through confirmations without reading them.
    if len(exact_active) == 1:
        return _accept(text, exact_active[0], relaxed=was_relaxed)
    if not exact_active and len(exact) == 1:
        return _accept(text, exact[0], relaxed=was_relaxed)

    # No current name matched, so fall back to former names — but only when
    # exactly one candidate claims the query as a name it used to hold. Two
    # companies having used the same name is a genuine choice for a human.
    if not exact:
        previous = [c for c in ranked if c.matched_previous_name]
        if len(previous) == 1:
            logger.info(
                "resolve %r: matched former name %r of %s",
                text,
                previous[0].matched_previous_name,
                previous[0].profile.number,
            )
            return _accept(text, previous[0], relaxed=was_relaxed)

    logger.info(
        "resolve %r: ambiguous, %d candidates (%d exact name matches)",
        text,
        len(ranked),
        len(exact),
    )
    return Resolution(kind=ResolutionKind.AMBIGUOUS, query=text, candidates=ranked)
