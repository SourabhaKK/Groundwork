"""The only way anything in this codebase talks to an LLM.

Two reasons it is a chokepoint rather than a convenience wrapper:

1. It makes the model-tiering claim in TECH_STACK.md structurally true. Callers
   ask for EXTRACTION or SYNTHESIS, not for a model name, so nobody can quietly
   start doing extraction on Sonnet.
2. It gives one place to log every call — which provider served it, which model,
   why a fallback happened. That log is the audit trail Phase 3.4 needs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from src.circuit_breaker import CircuitBreaker, CircuitOpenError
from src.config import Settings, get_settings
from src.llm.providers import AnthropicProvider, GeminiProvider, LLMProvider

logger = logging.getLogger(__name__)


class LLMTier(StrEnum):
    """What the call is *for*. The model is an implementation detail."""

    EXTRACTION = "extraction"
    SYNTHESIS = "synthesis"


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    used_fallback: bool


class AllProvidersFailedError(RuntimeError):
    """Primary was unavailable and the fallback failed too."""


class LLMRouter:
    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider,
        settings: Settings,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._settings = settings
        self._breaker = breaker or CircuitBreaker(
            name=primary.name,
            failure_threshold=settings.breaker_failure_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        )

    @property
    def breaker(self) -> CircuitBreaker:
        return self._breaker

    def _model_for(self, tier: LLMTier) -> str:
        return (
            self._settings.extraction_model
            if tier is LLMTier.EXTRACTION
            else self._settings.synthesis_model
        )

    async def complete(
        self,
        prompt: str,
        *,
        tier: LLMTier,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        tokens = max_tokens or self._settings.max_output_tokens
        model = self._model_for(tier)

        if self._breaker.allows_request():
            try:
                text = await self._primary.complete(
                    prompt, model=model, system=system, max_tokens=tokens
                )
            except Exception as exc:
                if not self._primary.is_transient(exc):
                    # Our fault. Surface it loudly instead of masking a bug in
                    # our own prompt as a provider outage.
                    logger.error("%s: non-transient error, not tripping breaker", model)
                    raise
                self._breaker.record_failure()
                logger.warning(
                    "%s failed transiently (%s); breaker=%s",
                    model,
                    type(exc).__name__,
                    self._breaker.state.value,
                )
            else:
                self._breaker.record_success()
                logger.info(
                    "llm call served by %s/%s (tier=%s)", self._primary.name, model, tier.value
                )
                return LLMResponse(
                    text=text, provider=self._primary.name, model=model, used_fallback=False
                )
        else:
            logger.info("breaker open; skipping %s and going straight to fallback", model)

        return await self._call_fallback(prompt, tier=tier, system=system, max_tokens=tokens)

    async def _call_fallback(
        self, prompt: str, *, tier: LLMTier, system: str | None, max_tokens: int
    ) -> LLMResponse:
        model = self._settings.fallback_model
        try:
            text = await self._fallback.complete(
                prompt, model=model, system=system, max_tokens=max_tokens
            )
        except Exception as exc:
            raise AllProvidersFailedError(
                f"primary unavailable and fallback {model} failed: {exc}"
            ) from exc

        logger.warning(
            "llm call served by FALLBACK %s/%s (tier=%s)", self._fallback.name, model, tier.value
        )
        return LLMResponse(text=text, provider=self._fallback.name, model=model, used_fallback=True)


_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    """Process-wide router. One breaker, so state is shared across all callers."""
    global _router
    if _router is None:
        settings = get_settings()
        _router = LLMRouter(
            primary=AnthropicProvider(settings.anthropic_api_key),
            fallback=GeminiProvider(settings.google_api_key),
            settings=settings,
        )
    return _router


def reset_router() -> None:
    """Test hook — drops the cached router so a fresh one is built."""
    global _router
    _router = None


__all__ = [
    "AllProvidersFailedError",
    "CircuitOpenError",
    "LLMResponse",
    "LLMRouter",
    "LLMTier",
    "get_router",
    "reset_router",
]
