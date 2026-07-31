"""Router behaviour, with both providers faked.

The load-bearing test here is `test_non_transient_error_does_not_trip_breaker`:
that distinction is the difference between a circuit breaker and a blanket
try/except, and getting it wrong means a bug in our own prompt masquerades as
an Anthropic outage.
"""

from __future__ import annotations

import pytest

from src.circuit_breaker import BreakerState, CircuitBreaker
from src.config import Settings
from src.llm.router import AllProvidersFailedError, LLMRouter, LLMTier
from tests.conftest import FakeClock


class FakeProvider:
    def __init__(self, name: str, *, transient_types: tuple[type[Exception], ...] = ()) -> None:
        self.name = name
        self.calls: list[dict[str, object]] = []
        self.raises: Exception | None = None
        self.reply = f"response from {name}"
        self._transient_types = transient_types

    async def complete(
        self, prompt: str, *, model: str, system: str | None, max_tokens: int
    ) -> str:
        self.calls.append({"prompt": prompt, "model": model, "system": system})
        if self.raises is not None:
            raise self.raises
        return self.reply

    def is_transient(self, exc: Exception) -> bool:
        return isinstance(exc, self._transient_types)


class Unavailable(Exception):
    """Stands in for a 5xx / timeout / rate limit."""


class BadRequest(Exception):
    """Stands in for a 400 caused by our own malformed prompt."""


def _router(
    settings: Settings, clock: FakeClock, threshold: int = 3
) -> tuple[LLMRouter, FakeProvider, FakeProvider]:
    primary = FakeProvider("anthropic", transient_types=(Unavailable,))
    fallback = FakeProvider("gemini", transient_types=(Unavailable,))
    breaker = CircuitBreaker(
        name="anthropic", failure_threshold=threshold, cooldown_seconds=60.0, clock=clock
    )
    return LLMRouter(primary, fallback, settings, breaker=breaker), primary, fallback


async def test_extraction_and_synthesis_use_different_models(
    settings: Settings, clock: FakeClock
) -> None:
    """The tiering claim in TECH_STACK.md is enforced here, not by convention."""
    router, primary, _ = _router(settings, clock)

    await router.complete("x", tier=LLMTier.EXTRACTION)
    await router.complete("y", tier=LLMTier.SYNTHESIS)

    assert primary.calls[0]["model"] == settings.extraction_model
    assert primary.calls[1]["model"] == settings.synthesis_model
    assert settings.extraction_model != settings.synthesis_model


async def test_primary_success_never_touches_fallback(settings: Settings, clock: FakeClock) -> None:
    router, _, fallback = _router(settings, clock)

    result = await router.complete("hello", tier=LLMTier.SYNTHESIS)

    assert result.provider == "anthropic"
    assert result.used_fallback is False
    assert fallback.calls == []


async def test_transient_failure_falls_back_to_gemini(settings: Settings, clock: FakeClock) -> None:
    router, primary, fallback = _router(settings, clock)
    primary.raises = Unavailable("503")

    result = await router.complete("hello", tier=LLMTier.SYNTHESIS)

    assert result.used_fallback is True
    assert result.provider == "gemini"
    assert result.model == settings.fallback_model
    assert len(fallback.calls) == 1


async def test_non_transient_error_does_not_trip_breaker(
    settings: Settings, clock: FakeClock
) -> None:
    """A 400 is our bug. It must surface, not silently demote us to the fallback."""
    router, primary, fallback = _router(settings, clock)
    primary.raises = BadRequest("malformed schema")

    with pytest.raises(BadRequest):
        await router.complete("hello", tier=LLMTier.EXTRACTION)

    assert router.breaker.state is BreakerState.CLOSED
    assert router.breaker.consecutive_failures == 0
    assert fallback.calls == [], "must not quietly answer from the fallback model"


async def test_breaker_opens_then_skips_primary_entirely(
    settings: Settings, clock: FakeClock
) -> None:
    router, primary, fallback = _router(settings, clock, threshold=3)
    primary.raises = Unavailable("timeout")

    for _ in range(3):
        await router.complete("hello", tier=LLMTier.EXTRACTION)

    assert router.breaker.state is BreakerState.OPEN
    calls_before = len(primary.calls)

    await router.complete("hello", tier=LLMTier.EXTRACTION)

    assert len(primary.calls) == calls_before, "open breaker must not re-attempt a dead provider"
    assert len(fallback.calls) == 4


async def test_primary_is_retried_after_cooldown(settings: Settings, clock: FakeClock) -> None:
    router, primary, _ = _router(settings, clock, threshold=1)
    primary.raises = Unavailable("503")
    await router.complete("hello", tier=LLMTier.EXTRACTION)
    assert router.breaker.state is BreakerState.OPEN

    primary.raises = None
    clock.advance(60.0)

    result = await router.complete("hello", tier=LLMTier.EXTRACTION)

    assert result.used_fallback is False
    assert router.breaker.state is BreakerState.CLOSED


async def test_both_providers_down_raises(settings: Settings, clock: FakeClock) -> None:
    router, primary, fallback = _router(settings, clock)
    primary.raises = Unavailable("503")
    fallback.raises = Unavailable("503")

    with pytest.raises(AllProvidersFailedError):
        await router.complete("hello", tier=LLMTier.SYNTHESIS)


async def test_system_prompt_is_passed_through(settings: Settings, clock: FakeClock) -> None:
    router, primary, _ = _router(settings, clock)

    await router.complete("hello", tier=LLMTier.SYNTHESIS, system="be terse")

    assert primary.calls[0]["system"] == "be terse"
