"""Direct tests for the provider adapters (Phase 2.6, closing Phase 0's debt).

`is_transient()` is the smallest and most load-bearing judgement in the
codebase. Everything the circuit breaker does — the LLM router's, and now the
two per-source breakers Phase 2.4 added on the same pattern — rests on it being
right. Get it wrong in one direction and a bug in our own prompt looks like an
Anthropic outage, silently demoting every later call to the fallback model while
we debug the wrong system. Get it wrong in the other and a real outage never
trips the breaker, so every request pays a full timeout to rediscover it.

Until now this was exercised only through `FakeProvider` in the router tests,
which pins our *intent* but not our reading of somebody else's exception
hierarchy. These tests construct **real SDK exception objects**, so they fail if
Anthropic or Google moves `status_code`, renames a class, or changes what it
raises. That couples them to SDK versions on purpose: this is precisely the
assumption whose breakage nobody would otherwise notice until a live run.

No network. The SDK exceptions are built directly and the one `complete()` test
replaces the SDK client with a stub.
"""

from __future__ import annotations

import anthropic
import httpx
import pytest
from google.genai import errors as genai_errors

from src.llm.providers import AnthropicProvider, GeminiProvider, ProviderRefusalError


def _response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1"))


def _anthropic() -> AnthropicProvider:
    return AnthropicProvider(api_key="test-key-not-real")


def _gemini() -> GeminiProvider:
    return GeminiProvider(api_key="test-key-not-real")


# --------------------------------------------------------------------------
# Anthropic: what counts as "the provider is down"
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [500, 502, 503, 529])
def test_anthropic_server_errors_are_transient(status: int) -> None:
    error = anthropic.APIStatusError("boom", response=_response(status), body=None)
    assert _anthropic().is_transient(error) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_anthropic_client_errors_are_not_transient(status: int) -> None:
    """Our fault, not theirs. A malformed prompt must never trip the breaker —
    otherwise a schema bug demotes every later call to the fallback model and
    the outage we go looking for was never there."""
    error = anthropic.APIStatusError("bad request", response=_response(status), body=None)
    assert _anthropic().is_transient(error) is False


def test_anthropic_rate_limit_is_transient() -> None:
    """429 is the provider telling us to back off — worth a cooldown, unlike a
    400, which no amount of waiting will fix."""
    error = anthropic.RateLimitError("slow down", response=_response(429), body=None)
    assert _anthropic().is_transient(error) is True


def test_anthropic_connection_and_timeout_errors_are_transient() -> None:
    request = httpx.Request("POST", "https://api.anthropic.com/v1")
    provider = _anthropic()

    assert provider.is_transient(anthropic.APIConnectionError(request=request)) is True
    assert provider.is_transient(anthropic.APITimeoutError(request=request)) is True


def test_a_refusal_is_not_an_outage() -> None:
    """The model declining on policy grounds says nothing about whether the
    provider is up. Tripping on it would take the primary model offline for a
    minute over one prompt it didn't like."""
    assert _anthropic().is_transient(ProviderRefusalError("declined")) is False


def test_an_unrelated_exception_is_not_transient() -> None:
    """The default has to be "not an outage". A breaker that trips on anything
    unrecognised would treat our own bugs as provider failures by default."""
    assert _anthropic().is_transient(ValueError("something else entirely")) is False


# --------------------------------------------------------------------------
# Anthropic: the refusal path in complete()
# --------------------------------------------------------------------------


class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Message:
    def __init__(self, *, stop_reason: str | None, content: list[_Block]) -> None:
        self.stop_reason = stop_reason
        self.content = content


class _FakeMessages:
    def __init__(self, message: _Message) -> None:
        self._message = message
        self.kwargs: dict[str, object] = {}

    async def create(self, **kwargs: object) -> _Message:
        self.kwargs = kwargs
        return self._message


def _with_message(provider: AnthropicProvider, message: _Message) -> _FakeMessages:
    messages = _FakeMessages(message)
    provider._client = type("_Client", (), {"messages": messages})()  # noqa: SLF001
    return messages


@pytest.mark.asyncio
async def test_a_refusal_raises_rather_than_returning_empty_text() -> None:
    """A refused response has empty `content`. Without the explicit check this
    would return "" — an empty report section that reads as "nothing was found
    about this company" rather than "the model declined to answer"."""
    provider = _anthropic()
    _with_message(provider, _Message(stop_reason="refusal", content=[]))

    with pytest.raises(ProviderRefusalError):
        await provider.complete("prompt", model="claude-haiku-4-5", system=None, max_tokens=100)


@pytest.mark.asyncio
async def test_text_blocks_are_concatenated() -> None:
    provider = _anthropic()
    _with_message(
        provider,
        _Message(stop_reason="end_turn", content=[_Block("first "), _Block("second")]),
    )

    text = await provider.complete("prompt", model="m", system=None, max_tokens=100)
    assert text == "first second"


@pytest.mark.asyncio
async def test_no_system_prompt_means_the_key_is_absent_not_none() -> None:
    """The SDK rejects `system=None`; the adapter has to omit the key entirely.
    A silent 400 here would look to the breaker like our own malformed request —
    correctly, but the report would just be missing a section."""
    provider = _anthropic()
    messages = _with_message(provider, _Message(stop_reason="end_turn", content=[_Block("x")]))

    await provider.complete("p", model="m", system=None, max_tokens=100)
    assert "system" not in messages.kwargs

    await provider.complete("p", model="m", system="be terse", max_tokens=100)
    assert messages.kwargs["system"] == "be terse"


# --------------------------------------------------------------------------
# Gemini: the fallback's classification must match, or failover is asymmetric
# --------------------------------------------------------------------------


@pytest.mark.parametrize("code", [500, 502, 503, 429])
def test_gemini_server_errors_and_rate_limits_are_transient(code: int) -> None:
    error = genai_errors.APIError(code, {"error": {"message": "unavailable"}})
    assert _gemini().is_transient(error) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404])
def test_gemini_client_errors_are_not_transient(code: int) -> None:
    error = genai_errors.APIError(code, {"error": {"message": "bad request"}})
    assert _gemini().is_transient(error) is False


def test_gemini_transport_errors_are_transient() -> None:
    provider = _gemini()
    assert provider.is_transient(TimeoutError("timed out")) is True
    assert provider.is_transient(ConnectionError("refused")) is True


def test_gemini_ignores_unrelated_exceptions() -> None:
    assert _gemini().is_transient(ValueError("nope")) is False


def test_both_providers_agree_on_what_an_outage_is() -> None:
    """Not a style check. If the fallback classified a 500 as our fault, a real
    Google outage would surface as an unhandled error instead of a stated
    failure — and Phase 5.7 already requires the two providers to be
    interchangeable in shape. They have to agree on failure too."""
    anthropic_500 = anthropic.APIStatusError("x", response=_response(500), body=None)
    gemini_500 = genai_errors.APIError(500, {"error": {"message": "x"}})

    assert _anthropic().is_transient(anthropic_500) is _gemini().is_transient(gemini_500) is True

    anthropic_400 = anthropic.APIStatusError("x", response=_response(400), body=None)
    gemini_400 = genai_errors.APIError(400, {"error": {"message": "x"}})

    assert _anthropic().is_transient(anthropic_400) is _gemini().is_transient(gemini_400) is False
