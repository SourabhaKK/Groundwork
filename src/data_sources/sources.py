"""Source tagging and the authority taxonomy (Phases 2.3 and 2.3a).

Everything gathered — from the register or the web — becomes a `TaggedDocument`
carrying its URL, its source type, and when it was retrieved. One type for both
sources, because Phase 5.4 scores a *mixed* list per section: "at least one
scored claim is `official_registry` or `trade_press`" is a question asked across
register and web evidence together. Two shapes would mean every consumer writes
a merge step, and the merge is where a source type gets dropped.

That a document cannot be constructed without a URL and a timestamp is the
point. The project's non-negotiable is that traceability is a *structural*
property of the data model rather than something prompting is trusted to
enforce, and this is where that structure lives.

**The taxonomy (2.3a), highest authority first:**

- `official_registry` — Companies House, the FCA, the PRA, and other independent
  public bodies. No stake in what they report about the company.
- `official_self` — the company's own disclosures. Primary and often audited,
  but self-interested.
- `trade_press` — recognised UK business and trade outlets.
- `review` — review and ratings platforms.
- `general_web` — everything else, and the default. Nothing is discarded for
  being unclassifiable; the bottom tier is what it is for.

**Boundary, stated so a later session doesn't go looking for it twice: this
module assigns `official_self` as a label and nothing more.** It answers "whose
voice is this?" 2.3a's further condition — that an `official_self` claim counts
toward Phase 5.4's HIGH-band authority gate only when the same section also
holds a non-self claim — is evaluated **entirely in `score_confidence()`**,
because it is a property of a section's whole claim set, not of any one
document. This module never sees a section's other claims and must not try to.
A document tagged `official_self` carries no implication about whether it will
count; that verdict does not exist until scoring.

**Classification is a pure function, deliberately.** The taxonomy is a small
closed set matched against a domain, and its output feeds the rule the entire
uncertainty story rests on. An LLM tagger would make a section's confidence band
able to move between two runs with identical evidence — the same failure the
Phase 4.1 granularity rule exists to prevent on the claim-count side — while
costing a model call per document to do a lookup.

**Known limitation:** an allowlist is maintenance, and it will always be
slightly out of date. A genuine UK trade outlet that isn't listed is tagged
`general_web` and counts for less than it should. That is the safe direction —
it understates confidence rather than overstating it — but the taxonomy's
accuracy is bounded by a hand-written list, and that belongs in the design note
rather than being presented as a general solution.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from src.data_sources.companies_house import CompanyProfile
from src.data_sources.failures import GatherFailure
from src.data_sources.registry_gather import RegistryGather
from src.data_sources.web_gather import WebGather, WebSection, trading_name

logger = logging.getLogger(__name__)

# The public register, not the API. `api.company-information.service.gov.uk`
# needs a key and answers in JSON — a citation an underwriter clicks and cannot
# read is not a citation. These paths were verified live against company
# 09446231 rather than reconstructed from memory, because inventing a
# plausible-looking source URL is the one thing this project treats as a hard
# boundary.
REGISTER_BASE = "https://find-and-update.company-information.service.gov.uk"


class SourceType(StrEnum):
    """Authority tier. Order of declaration is the order of authority."""

    OFFICIAL_REGISTRY = "official_registry"
    OFFICIAL_SELF = "official_self"
    TRADE_PRESS = "trade_press"
    REVIEW = "review"
    GENERAL_WEB = "general_web"


# Independent public bodies. `gov.uk` is included as a whole rather than
# enumerated department by department: a .gov.uk domain is by definition a UK
# public body, which is exactly the property this tier is asserting — an
# institution with no stake in what it reports about the company.
REGISTRY_DOMAINS = frozenset(
    {
        "gov.uk",
        "fca.org.uk",
        "bankofengland.co.uk",
        "psr.org.uk",
        "ico.org.uk",
        "ofcom.org.uk",
        "financial-ombudsman.org.uk",
        "fscs.org.uk",
        "sra.org.uk",
        "cqc.org.uk",
    }
)

TRADE_PRESS_DOMAINS = frozenset(
    {
        # National business desks
        "ft.com",
        "reuters.com",
        "bloomberg.com",
        "bbc.co.uk",
        "thetimes.co.uk",
        "telegraph.co.uk",
        "theguardian.com",
        "economist.com",
        "independent.co.uk",
        "standard.co.uk",
        "cityam.com",
        "thisismoney.co.uk",
        "sky.com",
        # Sector and trade
        "altfi.com",
        "fintechfutures.com",
        "finextra.com",
        "thebanker.com",
        "bankingtech.com",
        "sifted.eu",
        "techcrunch.com",
        "uktech.news",
        "businesscloud.co.uk",
        "insidermedia.com",
        "thegrocer.co.uk",
        "retailgazette.co.uk",
        "constructionnews.co.uk",
        "computerweekly.com",
        "accountancyage.com",
    }
)

REVIEW_DOMAINS = frozenset(
    {
        "trustpilot.com",
        "feefo.com",
        "reviews.io",
        "sitejabber.com",
        "reviewcentre.com",
        "glassdoor.co.uk",
        "glassdoor.com",
        "indeed.co.uk",
        "indeed.com",
        "play.google.com",
        "apps.apple.com",
        "yelp.co.uk",
        "yelp.com",
        "checkatrade.com",
        "trustatrader.com",
    }
)

# Multi-part public suffixes we need to see past to find the registrable label —
# "monzo" in monzo.co.uk, not "co". Hand-listed rather than pulling in
# `tldextract`: suffix matching covers every domain in the lists above without a
# new dependency, and TECH_STACK.md is locked, so a dependency would have to be
# logged as a deviation to buy nothing.
_COMPOUND_SUFFIXES = (".co.uk", ".org.uk", ".ac.uk", ".gov.uk", ".ltd.uk", ".plc.uk", ".me.uk")

# Name tokens too generic to identify a company by. Without this, a company
# called "The Bank Group" would claim bank.com as its own site.
_GENERIC_NAME_TOKENS = frozenset(
    {
        "the",
        "uk",
        "group",
        "holdings",
        "bank",
        "company",
        "services",
        "solutions",
        "trading",
        "international",
        "global",
        "digital",
        "partners",
        "capital",
        "finance",
    }
)


def _hostname(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def _in(host: str, domains: frozenset[str]) -> bool:
    """Domain-or-subdomain match, so `register.fca.org.uk` matches `fca.org.uk`.

    Anchored on a dot rather than a bare suffix test: `notfca.org.uk` must not
    match `fca.org.uk`, and `endswith` alone would say it does.
    """
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def registrable_label(host: str) -> str:
    """The label that identifies the owner: `monzo` from monzo.com or monzo.co.uk."""
    if not host:
        return ""
    for suffix in _COMPOUND_SUFFIXES:
        if host.endswith(suffix):
            return host[: -len(suffix)].split(".")[-1]
    parts = host.split(".")
    return parts[-2] if len(parts) >= 2 else parts[0]


def is_company_own_domain(host: str, profile: CompanyProfile) -> bool:
    """Does this domain look like it belongs to the company itself?

    A heuristic, and knowingly so: Companies House holds no website field, so
    there is nothing authoritative to compare a domain against. It matches the
    registrable label against the company's name with legal form stripped —
    `monzo.com` against "MONZO BANK LIMITED".

    It is acceptable *because both error directions are conservative*. A false
    positive demotes a source out of the HIGH-band authority gate unless the
    section is corroborated; a false negative leaves the company's own site as
    `general_web`, the lowest weight. The heuristic can withhold authority. It
    cannot invent it.
    """
    label = registrable_label(host).replace("-", "")
    if not label:
        return False

    name = trading_name(profile.name).lower()
    tokens = [t for t in re.split(r"\W+", name) if t]
    if not tokens:
        return False

    compact = "".join(tokens)
    if label == compact:
        return True

    # A single distinctive token is enough — press and the company itself both
    # shorten "MONZO BANK LIMITED" to "Monzo". A generic one is not, or every
    # company with "Group" in its name would claim group.com.
    first = tokens[0]
    return label == first and first not in _GENERIC_NAME_TOKENS and len(first) >= 4


def classify_source(url: str, profile: CompanyProfile | None = None) -> SourceType:
    """Which authority tier this URL belongs to. Never raises; defaults low.

    Order matters in two places. Registry is checked first, because an FCA
    enforcement notice *about* a company is the regulator's voice, not the
    company's. Self is checked before press, because when the subject of the
    report is itself a media organisation, its own site is its own voice — the
    conservative reading, and the one that keeps the corroboration gate
    meaningful.
    """
    host = _hostname(url)
    if not host:
        # A document with no resolvable host cannot be cited, but it is not this
        # function's job to drop it — it reports the lowest tier and lets the
        # caller decide.
        logger.warning("unparseable source URL, tagging general_web: %r", url)
        return SourceType.GENERAL_WEB

    if _in(host, REGISTRY_DOMAINS):
        return SourceType.OFFICIAL_REGISTRY
    if profile is not None and is_company_own_domain(host, profile):
        return SourceType.OFFICIAL_SELF
    if _in(host, REVIEW_DOMAINS):
        return SourceType.REVIEW
    if _in(host, TRADE_PRESS_DOMAINS):
        return SourceType.TRADE_PRESS
    return SourceType.GENERAL_WEB


class TaggedDocument(BaseModel):
    """One piece of evidence, tagged and traceable.

    Every field that makes a claim citable is required at construction. A
    document that cannot say where it came from and when cannot be built, which
    is what makes traceability structural rather than a prompting convention.
    """

    url: str
    source_type: SourceType
    retrieved_at: datetime
    content: str
    title: str | None = None
    section: WebSection | None = None  # None for register documents, which serve all three
    origin: str = ""  # which endpoint or search produced this, for the 3.4 audit log


class EvidenceBundle(BaseModel):
    """Everything gathered for one company, tagged, plus every stated gap.

    Documents and failures travel together deliberately. Phase 5 has to render
    both, and a bundle that carried only what was found would let a missing
    source read as an absent fact — the failure mode this whole layer exists to
    prevent.
    """

    company_number: str
    company_name: str
    documents: list[TaggedDocument] = Field(default_factory=list)
    failures: list[GatherFailure] = Field(default_factory=list)

    @property
    def is_complete(self) -> bool:
        return not self.failures

    def for_section(self, section: WebSection) -> list[TaggedDocument]:
        """Documents relevant to one section: its own, plus every register
        document, since the register serves all three sections."""
        return [d for d in self.documents if d.section is section or d.section is None]

    def count_by_type(self) -> dict[SourceType, int]:
        counts: dict[SourceType, int] = {}
        for document in self.documents:
            counts[document.source_type] = counts.get(document.source_type, 0) + 1
        return counts


# --------------------------------------------------------------------------
# Rendering the register into documents
# --------------------------------------------------------------------------


def _profile_text(profile: CompanyProfile) -> str:
    parts = [
        f"{profile.name} (company number {profile.number})",
        f"status: {profile.status or 'unknown'}",
        f"type: {profile.company_type or 'unknown'}",
    ]
    if profile.incorporated_on:
        parts.append(f"incorporated: {profile.incorporated_on.isoformat()}")
    if profile.ceased_on:
        parts.append(f"ceased: {profile.ceased_on.isoformat()}")
    if profile.sic_codes:
        parts.append(f"SIC codes: {', '.join(profile.sic_codes)}")
    if profile.registered_office_address:
        parts.append(f"registered office: {profile.registered_office_address.one_line()}")
    if profile.has_insolvency_history:
        parts.append("has insolvency history: yes")
    if profile.previous_names:
        formerly = "; ".join(p.name for p in profile.previous_names)
        parts.append(f"previously registered as: {formerly}")
    return "\n".join(parts)


def registry_documents(gather: RegistryGather) -> list[TaggedDocument]:
    """The register as up to four documents — profile, filings, officers, PSC.

    One document per *endpoint*, not per record. A hundred filings rendered as a
    hundred documents would all carry the same citation URL, inflating apparent
    evidence volume without adding a single independent source — and Phase 5.4
    scores on claim counts drawn from these. The granularity that matters is
    defined at the claim level in Phase 4.1, not here.
    """
    number = gather.company_number
    base = f"{REGISTER_BASE}/company/{number}"
    stamp = gather.retrieved_at
    documents = [
        TaggedDocument(
            url=base,
            source_type=SourceType.OFFICIAL_REGISTRY,
            retrieved_at=stamp,
            title=f"{gather.company_name} — company profile",
            content=_profile_text(gather.profile),
            origin="companies_house:profile",
        )
    ]

    if gather.filings:
        lines = [
            " — ".join(
                filter(
                    None,
                    [
                        f.filed_on.isoformat() if f.filed_on else None,
                        f.category,
                        f.description or f.filing_type,
                    ],
                )
            )
            for f in gather.filings
        ]
        header = f"{len(gather.filings)} most recent filings"
        if gather.filings_truncated:
            header += f" (of {gather.filings_total} on the register)"
        documents.append(
            TaggedDocument(
                url=f"{base}/filing-history",
                source_type=SourceType.OFFICIAL_REGISTRY,
                retrieved_at=stamp,
                title=f"{gather.company_name} — filing history",
                content=header + ":\n" + "\n".join(lines),
                origin="companies_house:filing_history",
            )
        )

    if gather.officers:
        lines = [
            " — ".join(
                filter(
                    None,
                    [
                        o.name,
                        o.role,
                        f"appointed {o.appointed_on.isoformat()}" if o.appointed_on else None,
                        f"resigned {o.resigned_on.isoformat()}" if o.resigned_on else "current",
                        o.occupation,
                    ],
                )
            )
            for o in gather.officers
        ]
        active = sum(1 for o in gather.officers if o.is_active)
        documents.append(
            TaggedDocument(
                url=f"{base}/officers",
                source_type=SourceType.OFFICIAL_REGISTRY,
                retrieved_at=stamp,
                title=f"{gather.company_name} — officers",
                content=(
                    f"{active} current and {len(gather.officers) - active} former "
                    f"officer appointments:\n" + "\n".join(lines)
                ),
                origin="companies_house:officers",
            )
        )

    if gather.psc:
        lines = [
            " — ".join(
                filter(
                    None,
                    [
                        p.name or f"[{p.kind or 'unnamed'}]",
                        p.kind,
                        ", ".join(p.natures_of_control) or None,
                        f"notified {p.notified_on.isoformat()}" if p.notified_on else None,
                        f"ceased {p.ceased_on.isoformat()}" if p.ceased_on else None,
                    ],
                )
            )
            for p in gather.psc
        ]
        documents.append(
            TaggedDocument(
                url=f"{base}/persons-with-significant-control",
                source_type=SourceType.OFFICIAL_REGISTRY,
                retrieved_at=stamp,
                title=f"{gather.company_name} — persons with significant control",
                content="\n".join(lines),
                origin="companies_house:psc",
            )
        )

    return documents


def web_documents(gather: WebGather, profile: CompanyProfile) -> list[TaggedDocument]:
    """One document per search result, tagged by domain and by section.

    The profile is passed so `official_self` can be recognised — it is the only
    thing that distinguishes the company's own site from any other website.
    """
    documents: list[TaggedDocument] = []
    for search in gather.sections:
        for result in search.results:
            documents.append(
                TaggedDocument(
                    url=result.url,
                    source_type=classify_source(result.url, profile),
                    retrieved_at=result.retrieved_at,
                    title=result.title,
                    content=result.content,
                    section=search.section,
                    origin=f"tavily:{search.section.value}",
                )
            )
    return documents


def build_evidence(
    profile: CompanyProfile,
    registry: RegistryGather | None = None,
    web: WebGather | None = None,
) -> EvidenceBundle:
    """Merge both gathers into one tagged, traceable evidence bundle.

    Either gather may be absent — a run where the register was reachable and
    Tavily was not still produces a bundle, and the gap travels with it in
    `failures`. That asymmetry is the normal case, not an edge case.
    """
    documents: list[TaggedDocument] = []
    failures: list[GatherFailure] = []

    if registry is not None:
        documents.extend(registry_documents(registry))
        failures.extend(registry.failures)
    if web is not None:
        documents.extend(web_documents(web, profile))
        failures.extend(web.failures)

    bundle = EvidenceBundle(
        company_number=profile.number,
        company_name=profile.name,
        documents=documents,
        failures=failures,
    )
    logger.info(
        "evidence %s: %d document(s) %s, %d gap(s)",
        profile.number,
        len(documents),
        {t.value: n for t, n in bundle.count_by_type().items()},
        len(failures),
    )
    return bundle
