"""Provider adapters behind one interface.

Each adapter owns two things: how to make the call, and which of its own errors
count as "the provider is down" versus "we sent something invalid". That second
judgement is what keeps the circuit breaker meaningful.
"""

from __future__ import annotations

from typing import Protocol

import anthropic
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types


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

        response = await self._client.messages.create(**kwargs)  # type: ignore[arg-type]

        # A refusal is a content outcome, not an exception — check it before
        # indexing into content, which is empty when the request was declined.
        if response.stop_reason == "refusal":
            raise ProviderRefusalError(f"{model} declined the request")

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
