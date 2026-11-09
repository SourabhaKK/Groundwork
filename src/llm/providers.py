"""Provider adapters behind one interface.

Each adapter owns two things: how to make the call, and which of its own errors
count as "the provider is down" versus "we sent something invalid". That second
judgement is what keeps the circuit breaker meaningful.
"""

from __future__ import annotations

import logging
from typing import Protocol

import anthropic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

logger = logging.getLogger(__name__)


class ProviderRefusalError(RuntimeError):
    """The model declined on policy grounds. Not a provider outage — do not trip."""


class LLMProvider(Protocol):
    """Minimal surface the router depends on."""

    name: str

    async def complete(
        self, prompt: str, *, model: str, system: str | None, max_tokens: int
    ) -> str: ...

    def is_transient(self, exc: Exception) -> bool:
        """True if `exc` means the provider is unavailable, not that we sent junk."""
        ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str) -> None:
        self._client = anthropic.AsyncAnthropic(api_key=api_key)

    async def complete(
        self, prompt: str, *, model: str, system: str | None, max_tokens: int
    ) -> str:
        kwargs: dict[str, object] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            kwargs["system"] = system

        response = await self._client.messages.create(**kwargs)  # type: ignore[call-overload]

        # A refusal is a content outcome, not an exception — check it before
        # indexing into content, which is empty when the request was declined.
        if response.stop_reason == "refusal":
            raise ProviderRefusalError(f"{model} declined the request")

        # Truncation is not an exception — it is a 200 with a short answer, and
        # it cost the Phase 5 live pass a whole report before anyone saw it:
        # every synthesis call hit the cap, returned unterminated JSON, and the
        # only visible symptom was three sections saying synthesis did not
        # complete. `max_tokens` bounds thinking *and* text together, and models
        # that think by default spend part of the budget before writing
        # anything, so this is a live risk whenever a cap is tuned. Logged
        # rather than raised: a truncated response is still the caller's to
        # judge, and callers already degrade honestly on unparseable output.
        if response.stop_reason == "max_tokens":
            logger.warning(
                "%s hit the max_tokens cap (%d) — output is truncated", model, max_tokens
            )

        return "".join(block.text for block in response.content if block.type == "text")

    def is_transient(self, exc: Exception) -> bool:
        if isinstance(exc, anthropic.APIConnectionError | anthropic.APITimeoutError):
            return True
        if isinstance(exc, anthropic.RateLimitError):
            return True
        if isinstance(exc, anthropic.APIStatusError):
            return exc.status_code >= 500
        # BadRequestError, AuthenticationError, NotFoundError and friends are
        # our fault, not the provider's. Tripping on those would mean a bug in
        # our own prompt looks like an Anthropic outage.
        return False


class GeminiProvider:
    name = "gemini"

    def __init__(self, api_key: str) -> None:
        self._client = genai.Client(api_key=api_key)

    async def complete(
        self, prompt: str, *, model: str, system: str | None, max_tokens: int
    ) -> str:
        config = genai_types.GenerateContentConfig(
            max_output_tokens=max_tokens,
            system_instruction=system,
            # Thinking off. 2.5-generation models spend `max_output_tokens` on
            # internal reasoning before emitting any visible text — measured at
            # 602 thinking tokens against 15 visible ones for a one-sentence
            # answer capped at 2,000. The same cap therefore buys the fallback
            # far less report than it buys the primary, which is a truncation
            # asymmetry sitting exactly where Phase 5.7 has to demonstrate the
            # two providers produce structurally equivalent output.
            #
            # This is a fallback: its job is to keep the report the same shape
            # when Anthropic is unreachable, not to reason harder than the model
            # it stands in for. Cheaper and faster is a side benefit, not the
            # reason.
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        )
        response = await self._client.aio.models.generate_content(
            model=model, contents=prompt, config=config
        )
        return response.text or ""

    def is_transient(self, exc: Exception) -> bool:
        if isinstance(exc, genai_errors.APIError):
            code = getattr(exc, "code", None)
            return code is not None and (code == 429 or code >= 500)
        return isinstance(exc, TimeoutError | ConnectionError)
