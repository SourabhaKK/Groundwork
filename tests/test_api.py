"""End-to-end wiring of the gate: does /report actually refuse unauthenticated
callers, and is /health actually reachable without the token?

The second half matters as much as the first — the Phase 6.4a keep-warm cron
cannot carry the demo secret in a public workflow file.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.config import get_settings
from tests.conftest import TEST_TOKEN


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


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


def test_report_accepts_valid_token_and_returns_not_implemented(client: TestClient) -> None:
    response = client.post(
        "/report",
        json={"query": "Monzo Bank Ltd"},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )

    assert response.status_code == 501
    assert response.json()["query"] == "Monzo Bank Ltd"


def test_report_rejects_empty_query(client: TestClient) -> None:
    response = client.post(
        "/report",
        json={"query": ""},
        headers={"Authorization": f"Bearer {TEST_TOKEN}"},
    )
    assert response.status_code == 422


def test_rate_limit_applies_to_authenticated_callers(client: TestClient) -> None:
    """Being authenticated is not an exemption — five per hour, then 429."""
    headers = {"Authorization": f"Bearer {TEST_TOKEN}"}

    for _ in range(5):
        assert client.post("/report", json={"query": "Monzo"}, headers=headers).status_code == 501

    blocked = client.post("/report", json={"query": "Monzo"}, headers=headers)
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_unauthenticated_requests_do_not_consume_budget(client: TestClient) -> None:
    """Auth is checked before the limiter, so bots can't burn the demo budget."""
    for _ in range(10):
        client.post("/report", json={"query": "Monzo"})

    assert client.get("/health").json()["rate_limit"]["global_used"] == 0
