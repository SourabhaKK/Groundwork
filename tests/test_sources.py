"""Tests for source tagging and the authority taxonomy (Phases 2.3 and 2.3a).

Two things are being pinned. First, that the tiers mean what 2.3a says they
mean — particularly the `official_registry` / `official_self` split, which exists
because an independent regulator and a company's word about itself are not the
same kind of evidence, and treating them as one was a real spec bug found while
building the Phase 0.7 mock. Second, that every document is traceable by
construction: a claim's citation is only as good as the URL and timestamp
recorded here.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.data_sources.companies_house import CompanyProfile, Officer
from src.data_sources.failures import FailureKind, GatherFailure
from src.data_sources.registry_gather import RegistryGather, RegistrySource
from src.data_sources.sources import (
    REGISTER_BASE,
    SourceType,
    TaggedDocument,
    build_evidence,
    classify_source,
    is_company_own_domain,
    registrable_label,
    registry_documents,
)
from src.data_sources.tavily import SearchDepth, SearchResult
from src.data_sources.web_gather import SectionSearch, WebGather, WebSection

PROFILE = CompanyProfile.model_validate(
    {
        "company_number": "09446231",
        "company_name": "MONZO BANK LIMITED",
        "company_status": "active",
        "date_of_creation": "2015-02-18",
        "sic_codes": ["64191", "64999"],
        "previous_company_names": [{"name": "FOCUS FS LIMITED"}],
    }
)


# --------------------------------------------------------------------------
# The taxonomy (2.3a)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        # Independent public bodies — no stake in what they report.
        ("https://find-and-update.company-information.service.gov.uk/company/09446231", "registry"),
        ("https://www.fca.org.uk/news/press-releases/example", "registry"),
        ("https://register.fca.org.uk/s/firm?id=001", "registry"),  # subdomain
        ("https://www.bankofengland.co.uk/prudential-regulation/publication", "registry"),
        # The company's own voice.
        ("https://monzo.com/about/", "self"),
        ("https://www.monzo.com/annual-report/", "self"),
        # Recognised trade and business press.
        ("https://www.ft.com/content/example", "trade"),
        ("https://www.bbc.co.uk/news/business-123", "trade"),
        ("https://sifted.eu/articles/example", "trade"),
        # Review platforms.
        ("https://uk.trustpilot.com/review/monzo.com", "review"),
        ("https://play.google.com/store/apps/details?id=co.uk.getmondo", "review"),
        # Everything else, and the default.
        ("https://somerandomblog.example/post", "web"),
        ("https://medium.com/@someone/monzo-thoughts", "web"),
    ],
)
def test_domains_map_to_the_right_tier(url: str, expected: str) -> None:
    tiers = {
        "registry": SourceType.OFFICIAL_REGISTRY,
        "self": SourceType.OFFICIAL_SELF,
        "trade": SourceType.TRADE_PRESS,
        "review": SourceType.REVIEW,
        "web": SourceType.GENERAL_WEB,
    }
    assert classify_source(url, PROFILE) is tiers[expected]


def test_a_regulators_notice_about_the_company_is_not_self_reported() -> None:
    """The whole reason the single `official` tier was split. An FCA enforcement
    notice is the regulator's voice about the company, not the company's voice
    about itself — and the confidence rule weights those differently."""
    assert classify_source("https://www.fca.org.uk/news/monzo-fine", PROFILE) is (
        SourceType.OFFICIAL_REGISTRY
    )


def test_unclassified_domains_are_kept_not_dropped() -> None:
    """`general_web` is what the bottom tier is for. Discarding unrecognised
    sources would silently delete evidence and make reports look thinner than
    the gathering actually was."""
    assert classify_source("https://obscure-fintech-blog.example/x", PROFILE) is (
        SourceType.GENERAL_WEB
    )


def test_a_lookalike_domain_does_not_inherit_authority() -> None:
    """Anchored on a dot: `endswith` alone would let notfca.org.uk pass as the
    regulator, which is how a fake source acquires real authority."""
    assert classify_source("https://notfca.org.uk/statement", PROFILE) is SourceType.GENERAL_WEB


def test_classification_without_a_profile_never_claims_self() -> None:
    """Self-detection needs a company to compare against; with none, the tag it
    cannot justify is the one it must not apply."""
    assert classify_source("https://monzo.com/about/") is SourceType.GENERAL_WEB


def test_an_unparseable_url_degrades_rather_than_raising() -> None:
    assert classify_source("not a url at all", PROFILE) is SourceType.GENERAL_WEB


# --------------------------------------------------------------------------
# The official_self heuristic, and its deliberate conservatism
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("monzo.com", "monzo"),
        ("monzo.co.uk", "monzo"),
        ("investors.monzo.com", "monzo"),
        ("example.org.uk", "example"),
        ("localhost", "localhost"),
    ],
)
def test_registrable_label_sees_past_compound_suffixes(host: str, expected: str) -> None:
    assert registrable_label(host) == expected


def test_self_detection_matches_the_full_name_or_a_distinctive_token() -> None:
    assert is_company_own_domain("monzo.com", PROFILE)  # distinctive first token
    assert is_company_own_domain("monzobank.com", PROFILE)  # compacted full name
    assert is_company_own_domain("monzo-bank.com", PROFILE)  # hyphens ignored


def test_a_generic_name_token_cannot_claim_a_domain() -> None:
    """Without this, "The Bank Group Limited" would claim bank.com as its own
    site — inventing an official_self source out of a common word."""
    generic = CompanyProfile.model_validate(
        {"company_number": "00000001", "company_name": "BANK GROUP LIMITED"}
    )
    assert not is_company_own_domain("bank.com", generic)


def test_an_unrelated_domain_is_not_the_companys_own() -> None:
    assert not is_company_own_domain("starlingbank.com", PROFILE)


def test_self_wins_over_press_when_the_company_is_a_publisher() -> None:
    """A report about a media company: its own site is its own voice. The
    conservative reading, and the one that keeps the corroboration gate
    meaningful — self-reporting must not be counted as independent coverage."""
    publisher = CompanyProfile.model_validate(
        {"company_number": "00000002", "company_name": "SIFTED LIMITED"}
    )
    assert classify_source("https://sifted.eu/articles/x", publisher) is SourceType.OFFICIAL_SELF
    # ...and for any other company, the same URL is ordinary trade press.
    assert classify_source("https://sifted.eu/articles/x", PROFILE) is SourceType.TRADE_PRESS


# --------------------------------------------------------------------------
# Traceability is structural, not a convention
# --------------------------------------------------------------------------


def test_a_document_cannot_exist_without_a_url_and_a_timestamp() -> None:
    """The non-negotiable: traceability is a property of the data model, not
    something prompting is trusted to enforce."""
    with pytest.raises(ValidationError):
        TaggedDocument(source_type=SourceType.GENERAL_WEB, content="x")  # type: ignore[call-arg]

    with pytest.raises(ValidationError):
        TaggedDocument(  # type: ignore[call-arg]
            url="https://x.test", source_type=SourceType.GENERAL_WEB, content="x"
        )


# --------------------------------------------------------------------------
# Rendering the register
# --------------------------------------------------------------------------


def _registry_gather(**overrides) -> RegistryGather:
    defaults = {
        "profile": PROFILE,
        "filings": [
            {"date": "2025-12-19", "category": "accounts", "description": "Accounts to 2025-02-28"}
        ],
        "officers": [Officer(name="ANIL, Tarun", role="director", appointed_on=None)],
        "psc": [{"name": "Monzo Bank Holding Group Limited", "kind": "corporate-entity"}],
        "retrieved_at": datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
    }
    return RegistryGather.model_validate(defaults | overrides)


def test_register_documents_cite_the_public_pages_not_the_api() -> None:
    """A citation an underwriter clicks and cannot read is not a citation — the
    API host needs a key and answers in JSON. These paths were verified against
    the live register for 09446231."""
    documents = registry_documents(_registry_gather())
    urls = {d.url for d in documents}

    assert urls == {
        f"{REGISTER_BASE}/company/09446231",
        f"{REGISTER_BASE}/company/09446231/filing-history",
        f"{REGISTER_BASE}/company/09446231/officers",
        f"{REGISTER_BASE}/company/09446231/persons-with-significant-control",
    }
    assert all(d.source_type is SourceType.OFFICIAL_REGISTRY for d in documents)
    assert all("api.company-information" not in d.url for d in documents)


def test_one_document_per_endpoint_not_per_record() -> None:
    """A hundred filings as a hundred documents would all carry the same
    citation URL — inflating apparent evidence volume without adding a single
    independent source, in a system that scores on claim counts."""
    many = [
        {"date": "2025-01-01", "category": "accounts", "description": f"Filing {i}"}
        for i in range(50)
    ]
    documents = registry_documents(_registry_gather(filings=many))

    filings = [d for d in documents if d.origin == "companies_house:filing_history"]
    assert len(filings) == 1
    assert "Filing 49" in filings[0].content


def test_truncation_is_visible_in_the_rendered_document() -> None:
    gather = _registry_gather(filings_total=400)
    filings = next(
        d for d in registry_documents(gather) if d.origin == "companies_house:filing_history"
    )
    assert "of 400 on the register" in filings.content


def test_an_empty_source_produces_no_document() -> None:
    """A document with no content would read as evidence that exists and says
    nothing — different from evidence that isn't there."""
    documents = registry_documents(_registry_gather(officers=[], psc=[]))
    origins = {d.origin for d in documents}

    assert "companies_house:officers" not in origins
    assert "companies_house:psc" not in origins
    assert "companies_house:profile" in origins  # the profile is always present


def test_the_profile_document_carries_former_names() -> None:
    """Phase 1 established that a former registered name is findable and a
    never-registered trading name is not. The one the register does hold has to
    survive into the evidence."""
    profile_doc = next(
        d for d in registry_documents(_registry_gather()) if d.origin == "companies_house:profile"
    )
    assert "FOCUS FS LIMITED" in profile_doc.content
    assert "64191" in profile_doc.content


# --------------------------------------------------------------------------
# Merging both gathers
# --------------------------------------------------------------------------


def _web_gather() -> WebGather:
    return WebGather(
        company_name=PROFILE.name,
        sections=[
            SectionSearch(
                section=WebSection.BUSINESS_MODEL,
                query="q",
                depth=SearchDepth.BASIC,
                results=[
                    SearchResult(url="https://monzo.com/about/", content="What we do"),
                    SearchResult(url="https://www.ft.com/content/x", content="Coverage"),
                ],
                credits_spent=1,
            ),
            SectionSearch(
                section=WebSection.REPUTATION,
                query="q",
                depth=SearchDepth.ADVANCED,
                results=[
                    SearchResult(url="https://uk.trustpilot.com/review/monzo.com", content="")
                ],
                credits_spent=2,
            ),
        ],
        failures=[
            GatherFailure(
                source=WebSection.COMPETITIVE_LANDSCAPE.value,
                kind=FailureKind.EXHAUSTED,
                detail="out of credits",
            )
        ],
    )


def test_both_gathers_merge_into_one_tagged_bundle() -> None:
    bundle = build_evidence(PROFILE, registry=_registry_gather(), web=_web_gather())
    counts = bundle.count_by_type()

    assert counts[SourceType.OFFICIAL_REGISTRY] == 4
    assert counts[SourceType.OFFICIAL_SELF] == 1
    assert counts[SourceType.TRADE_PRESS] == 1
    assert counts[SourceType.REVIEW] == 1


def test_gaps_travel_with_the_evidence() -> None:
    """A bundle carrying only what was found would let a missing source read as
    an absent fact — the failure this entire layer exists to prevent."""
    registry = _registry_gather(
        failures=[
            GatherFailure(
                source=RegistrySource.OFFICERS.value,
                kind=FailureKind.UNAVAILABLE,
                detail="503",
            )
        ]
    )
    bundle = build_evidence(PROFILE, registry=registry, web=_web_gather())

    assert not bundle.is_complete
    assert {f.source for f in bundle.failures} == {"officers", "competitive_landscape"}


def test_register_documents_serve_every_section() -> None:
    """Register evidence is not section-specific: incorporation date and SIC
    codes bear on business model, officer churn on quality signals. Partitioning
    it to one section would starve the others of the strongest source we have."""
    bundle = build_evidence(PROFILE, registry=_registry_gather(), web=_web_gather())

    business = bundle.for_section(WebSection.BUSINESS_MODEL)
    reputation = bundle.for_section(WebSection.REPUTATION)

    assert sum(1 for d in business if d.section is None) == 4
    assert sum(1 for d in reputation if d.section is None) == 4
    assert {d.url for d in business if d.section is not None} == {
        "https://monzo.com/about/",
        "https://www.ft.com/content/x",
    }


def test_a_bundle_survives_one_gather_being_absent() -> None:
    """Register up, Tavily down is the normal asymmetric case, not an edge one."""
    bundle = build_evidence(PROFILE, registry=_registry_gather(), web=None)

    assert len(bundle.documents) == 4
    assert bundle.is_complete


def test_web_documents_keep_their_retrieval_timestamp() -> None:
    """Stamped at retrieval, carried through unchanged — a timestamp rewritten
    at merge time would describe the report run, not the evidence."""
    web = _web_gather()
    stamped = web.sections[0].results[0].retrieved_at
    bundle = build_evidence(PROFILE, web=web)

    assert bundle.documents[0].retrieved_at == stamped
