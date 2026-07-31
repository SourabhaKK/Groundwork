"""Shared fixtures.

No test in this suite may touch a live API. Credentials here are obvious
placeholders so a leak into a real call fails fast and visibly.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from src.api import rate_limit
from src.config import Settings, get_settings
from src.data_sources.breakers import reset_breakers
from src.data_sources.search_quota import tracker
from src.llm import router as router_module

TEST_TOKEN = "test-demo-token-not-a-real-secret"


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Reset every process-global between tests so ordering can't leak state."""
    for key, value in {
        "COMPANIES_HOUSE_API_KEY": "test-ch-key",
        "TAVILY_API_KEY": "test-tavily-key",
        "ANTHROPIC_API_KEY": "test-anthropic-key",
        "GOOGLE_API_KEY": "test-google-key",
        "DEMO_ACCESS_TOKEN": TEST_TOKEN,
    }.items():
        monkeypatch.setenv(key, value)

    get_settings.cache_clear()
    rate_limit.reset_limiter()
    router_module.reset_router()
    reset_breakers()
    tracker.reset()

    yield

    get_settings.cache_clear()
    rate_limit.reset_limiter()
    router_module.reset_router()
    reset_breakers()
    tracker.reset()


@pytest.fixture
def settings() -> Settings:
    return get_settings()


class FakeClock:
    """Deterministic time. The breaker and limiter both take an injected clock
    so their tests assert on elapsed-time behaviour without sleeping."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
