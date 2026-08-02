"""End-to-end wiring of the gate: does /report actually refuse unauthenticated
callers, and is /health actually reachable without the token?

The second half matters as much as the first — the Phase 6.4a keep-warm cron
cannot carry the demo secret in a public workflow file.

Phase 6.1 added the endpoint's real behaviour to this file. Every test here
drives it through an overridden `get_report_runner`, which exists precisely so
these tests cannot reach a network — the first version of the endpoint had no
such seam and this suite immediately made a live Companies House call and got a
401 back. CI runs without secrets so that failure is loud there, but it should
never have been reachable at all.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient

from src.agent.claims import ClaimConfidence, Conflict, ConflictSeverity, EvidenceClaim
from src.agent.resolve import Candidate, ConfirmationReason, Resolution, ResolutionKind
from src.agent.state import ReportState, RunOutcome, decision
from src.api import report as report_module
from src.api.main import app
from src.config import get_settings
from src.data_sources.companies_house import CompanyAddress, CompanyProfile
from src.data_sources.sources import SourceType
from src.data_sources.web_gather import WebSection
from src.report.confidence import ConfidenceAssessment, ConfidenceBand, ConfidenceReason
from tests.conftest import TEST_TOKEN

AUTH = {"Authorization": f"Bearer {TEST_TOKEN}"}


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _profile(number: str = "09446231", name: str = "MONZO BANK LIMITED") -> CompanyProfile:
    return CompanyProfile(
        company_number=number,
        company_name=name,
        company_status="active",
        date_of_creation=date(2015, 2, 18),
        sic_codes=["64191"],
        registered_office_address=CompanyAddress(
            address_line_1="Broadwalk House", locality="London", postal_code="EC2A 2DA"
        ),
    )


def _claim(
    text: str = "The company is authorised by the FCA.",
    *,
    scores: bool = True,
    section: WebSection = WebSection.BUSINESS_MODEL,
) -> EvidenceClaim:
    return EvidenceClaim(
        text=text,
        source_url="https://www.fca.org.uk/firm/730427",
        source_type=SourceType.OFFICIAL_REGISTRY,
        section=section,
        confidence=ClaimConfidence.DIRECT,
        counts_toward_confidence=scores,
        exclusion_reason=None if scores else "quote not found in source",
    )


def _complete_state() -> ReportState:
    return ReportState(
        query="Monzo Bank Ltd",
        profile=_profile(),
        resolution=Resolution(kind=ResolutionKind.RESOLVED, query="Monzo Bank Ltd"),
        report="# Groundwork report\n\n## 1. Business model\n\nProse here.",
        outcome=RunOutcome.COMPLETE,
        claims=[_claim(), _claim("A second fact.", scores=False)],
        conflicts=[
            Conflict(
                severity=ConflictSeverity.MINOR,
                section=WebSection.BUSINESS_MODEL,
                claim_ids=["a", "b"],
                detail="Two sources give different customer counts.",
            )
        ],
        assessments={
            WebSection.BUSINESS_MODEL: ConfidenceAssessment(
                band=ConfidenceBand.HIGH,
                reason=ConfidenceReason.CORROBORATED,
                scored_claims=4,
                total_claims=6,
            )
        },
        decisions=[decision("resolve", "accepted one company", "unique exact match")],
        credits_spent=5,
        gather_rounds=1,
    )


@pytest.fixture
def runs() -> Iterator[list[ReportState]]:
    """Override the runner seam with a queue of states to return."""
    queue: list[ReportState] = []

    async def fake_runner(query: str) -> ReportState:
        return queue.pop(0) if queue else _complete_state()

    app.dependency_overrides[report_module.get_report_runner] = lambda: fake_runner
    yield queue
    app.dependency_overrides.clear()


def test_health_needs_no_token(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "llm_breaker" in body
    # Asserted against the configured value rather than a literal: this test's
    # job is that /health reports the budget, not what the budget is. The number
    # itself is pinned in test_web_gather, next to the credit arithmetic it is
    # derived from — where a change to it will fail for the right reason.
    assert body["rate_limit"]["global_budget"] == get_settings().global_report_budget
    assert body["search_quota"]["status"] == "unknown"
    assert body["search_quota"]["credits_this_container"] == 0


def test_health_exposes_no_secrets(client: TestClient) -> None:
    body = client.get("/health").text
    assert TEST_TOKEN not in body
    assert "test-anthropic-key" not in body


def test_report_rejects_missing_token(client: TestClient) -> None:
    response = client.post("/report", json={"query": "Monzo Bank Ltd"})
    assert response.status_code == 401


def test_report_rejects_wrong_token(client: TestClient) -> None:
    response = client.post(
        "/report",
        json={"query": "Monzo Bank Ltd"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert response.status_code == 401


def test_report_rejects_wrong_scheme(client: TestClient) -> None:
    response = client.post(
        "/report",
        json={"query": "Monzo Bank Ltd"},
        headers={"Authorization": f"Basic {TEST_TOKEN}"},
    )
    assert response.status_code == 401


def test_report_rejects_empty_query(client: TestClient) -> None:
    response = client.post(
        "/report",
        json={"query": ""},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 422


def test_rate_limit_applies_to_authenticated_callers(
    client: TestClient, runs: list[ReportState]
) -> None:
    """Being authenticated is not an exemption — five per hour, then 429."""
    for _ in range(5):
        assert client.post("/report", json={"query": "Monzo"}, headers=AUTH).status_code == 200

    blocked = client.post("/report", json={"query": "Monzo"}, headers=AUTH)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_unauthenticated_requests_do_not_consume_budget(client: TestClient) -> None:
    """Auth is checked before the limiter, so bots can't burn the demo budget."""
    for _ in range(10):
        client.post("/report", json={"query": "Monzo"})

    assert client.get("/health").json()["rate_limit"]["global_used"] == 0


# ---------------------------------------------------------------------------
# Phase 6.1 — the endpoint's real behaviour
# ---------------------------------------------------------------------------


def test_complete_run_returns_the_whole_deliverable(
    client: TestClient, runs: list[ReportState]
) -> None:
    """The markdown is the headline, not the whole of it.

    The decision log is the evidence for the agent-design criterion and the
    assessments are the uncertainty mechanism as data. A response carrying only
    `report` would make both invisible to anyone using the hosted demo.
    """
    response = client.post("/report", json={"query": "Monzo Bank Ltd"}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "complete"
    assert body["company_number"] == "09446231"
    assert body["report"].startswith("# Groundwork report")
    assert body["decisions"][0]["rationale"] == "unique exact match"
    assert body["conflicts"][0]["severity"] == "minor"
    assert body["credits_spent"] == 5
    # Claims travel with their provenance so a citation can be checked without
    # parsing the markdown.
    assert body["claims"][0]["source_url"] == "https://www.fca.org.uk/firm/730427"
    assert body["claims"][1]["counts_toward_confidence"] is False
    assert body["claims"][1]["exclusion_reason"] == "quote not found in source"


def test_the_api_never_recomputes_a_band(client: TestClient, runs: list[ReportState]) -> None:
    """The band is scored once, by the graph, and only rendered afterwards.

    Same discipline Phase 5 pinned on the badge and the working table, one layer
    out: the assessment here deliberately reports 4 scored of 6 while the state
    carries 2 claims. If anything in the API counted the list instead of reading
    the assessment, these numbers would come back as 1 and 2.
    """
    response = client.post("/report", json={"query": "Monzo"}, headers=AUTH)

    assessment = response.json()["assessments"]["business_model"]
    assert assessment["band"] == "high"
    assert assessment["reason"] == "corroborated"
    assert (assessment["scored_claims"], assessment["total_claims"]) == (4, 6)
    assert assessment["summary"] == "HIGH (corroborated, 4 scored of 6 claims)"


def test_needs_input_is_a_success_with_candidates(
    client: TestClient, runs: list[ReportState]
) -> None:
    """Disambiguation working is not an error.

    A 4xx here would make the frontend render the product's best behaviour as a
    failure — and the candidates carry `number`, which is what the client sends
    back to resolve the choice.
    """
    runs.append(
        ReportState(
            query="Monzo",
            outcome=RunOutcome.NEEDS_INPUT,
            resolution=Resolution(
                kind=ResolutionKind.NEEDS_CONFIRMATION,
                query="Monzo",
                profile=_profile(),
                reasons=[ConfirmationReason.PREVIOUS_NAME, ConfirmationReason.DISSOLVED],
                candidates=[
                    Candidate(profile=_profile(), score=3.0, match="exact"),
                    Candidate(
                        profile=_profile("12345678", "MONZO TRAVEL LIMITED"),
                        score=1.0,
                        match="partial",
                        matched_previous_name="MONDO MORTGAGES LIMITED",
                    ),
                ],
            ),
        )
    )

    response = client.post("/report", json={"query": "Monzo"}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "needs_input"
    assert body["report"] is None
    # Both reasons survive: a company found under a former name *and* dissolved
    # is both things, and reporting whichever was checked first hands back the
    # less alarming half at random.
    assert body["resolution"]["reasons"] == ["previous_name", "dissolved"]
    second = body["resolution"]["candidates"][1]
    assert second["number"] == "12345678"
    assert second["matched_previous_name"] == "MONDO MORTGAGES LIMITED"
    assert second["address"]
    assert second["incorporated_on"] == "2015-02-18"


def test_not_found_is_a_finding_not_an_error(client: TestClient, runs: list[ReportState]) -> None:
    """ "No such company on the register" is information about a company."""
    runs.append(
        ReportState(
            query="Nonexistent Widgets Xyzzy",
            outcome=RunOutcome.NOT_FOUND,
            resolution=Resolution(
                kind=ResolutionKind.NOT_FOUND,
                query="Nonexistent Widgets Xyzzy",
                detail="No company on the register matched that name.",
            ),
        )
    )

    response = client.post("/report", json={"query": "Nonexistent Widgets Xyzzy"}, headers=AUTH)

    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "not_found"
    assert body["resolution"]["detail"] == "No company on the register matched that name."


def test_no_evidence_is_still_a_200(client: TestClient, runs: list[ReportState]) -> None:
    """A thinly-documented company is the uncertainty feature, not a fault."""
    runs.append(
        ReportState(
            query="Tiny Ltd",
            profile=_profile("00000001", "TINY LIMITED"),
            outcome=RunOutcome.NO_EVIDENCE,
        )
    )

    response = client.post("/report", json={"query": "Tiny Ltd"}, headers=AUTH)

    assert response.status_code == 200
    assert response.json()["outcome"] == "no_evidence"


def test_provider_failure_is_the_one_http_error(
    client: TestClient, runs: list[ReportState]
) -> None:
    """ "Our providers are down" must never wear the same clothes as thin evidence.

    The same distinction `src/config.py` exists to preserve, at the HTTP layer:
    a client can tell the two apart without reading the body.
    """
    runs.append(
        ReportState(
            query="Monzo",
            outcome=RunOutcome.PROVIDER_FAILURE,
            error="all providers failed",
        )
    )

    response = client.post("/report", json={"query": "Monzo"}, headers=AUTH)

    assert response.status_code == 503
    assert "not a finding about the company" in response.json()["detail"]


def test_a_provider_failure_still_consumes_budget(
    client: TestClient, runs: list[ReportState]
) -> None:
    """It cost real calls, so it costs a report.

    Refunding it would let a caller retry a failing run without limit, which is
    the one path where retries are most likely and most expensive.
    """
    runs.append(ReportState(query="Monzo", outcome=RunOutcome.PROVIDER_FAILURE))
    client.post("/report", json={"query": "Monzo"}, headers=AUTH)

    assert client.get("/health").json()["rate_limit"]["global_used"] == 1


def test_the_default_runner_is_the_real_one() -> None:
    """The seam must not quietly become the production path.

    Every test above overrides the runner. If a refactor left the override in
    place as the default, they would all still pass against a service that never
    calls the graph.
    """
    assert report_module.get_report_runner() is report_module.run_with_real_dependencies
