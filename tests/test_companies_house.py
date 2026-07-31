"""Direct tests for `CompaniesHouseClient` (Phase 1.4).

These target the client in isolation. Everything else in the suite exercises it
only through `resolve_company`, which means a wrong auth scheme or a
mis-classified status code would surface three layers up as a puzzling
resolution outcome rather than at the source. The status classification is
load-bearing well beyond this module: "no such company" is a reportable finding,
"your key is wrong" must fail loudly at us, and "the register is down" must
never reach an underwriter dressed up as thin evidence.
"""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest

from src.data_sources.companies_house import (
    CompaniesHouseAuthError,
    CompaniesHouseClient,
    CompaniesHouseError,
    CompaniesHouseUnavailable,
    CompanyNotFound,
    InvalidCompanyQuery,
    normalise_company_number,
)


def _client_with(handler) -> CompaniesHouseClient:
    transport = httpx.AsyncClient(
        base_url="https://ch.test", transport=httpx.MockTransport(handler)
    )
    return CompaniesHouseClient(client=transport)


def _status_handler(status: int, headers: dict[str, str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, headers=headers or {}, json={})

    return handler


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_is_basic_with_key_as_username_and_blank_password() -> None:
    """Companies House uses HTTP Basic with the API key as the username and an
    empty password. Getting this wrong produces a 401 that looks identical to a
    revoked key, so it is worth pinning at the point the header is built."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"items": []})

    # Let the client build its own transport (and therefore its own auth), then
    # swap only the wire underneath it — so this asserts on what the client
    # constructs, not on what the test handed it.
    client = CompaniesHouseClient(api_key="secret-key")
    client._client._transport = httpx.MockTransport(handler)
    async with client:
        await client.search_companies("anything")

    scheme, _, encoded = seen["authorization"].partition(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "secret-key:"


# --------------------------------------------------------------------------
# Status classification — the distinction the rest of the system depends on
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_404_is_company_not_found() -> None:
    """A missing company is a fact about the world, and reportable as one."""
    async with _client_with(_status_handler(404)) as client:
        with pytest.raises(CompanyNotFound):
            await client.get_company_profile("09446231")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failures_are_not_transient(status: int) -> None:
    """Our misconfiguration, not the register's outage. It must not be
    swallowed as "unavailable" — that sends debugging at the wrong system."""
    async with _client_with(_status_handler(status)) as client:
        with pytest.raises(CompaniesHouseAuthError):
            await client.get_company_profile("09446231")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_server_errors_are_unavailable(status: int) -> None:
    async with _client_with(_status_handler(status)) as client:
        with pytest.raises(CompaniesHouseUnavailable):
            await client.get_company_profile("09446231")


@pytest.mark.asyncio
async def test_429_carries_retry_after() -> None:
    """Per 1.1's design the client does not retry — it surfaces the register's
    own Retry-After so Phase 2.4 can decide policy for every source at once."""
    async with _client_with(_status_handler(429, {"Retry-After": "42"})) as client:
        with pytest.raises(CompaniesHouseUnavailable) as raised:
            await client.get_company_profile("09446231")

    assert raised.value.retry_after == 42.0


@pytest.mark.asyncio
async def test_429_without_retry_after_is_still_unavailable() -> None:
    """A missing or non-numeric header must not turn a rate limit into a crash."""
    async with _client_with(_status_handler(429)) as client:
        with pytest.raises(CompaniesHouseUnavailable) as raised:
            await client.get_company_profile("09446231")

    assert raised.value.retry_after is None


@pytest.mark.asyncio
async def test_connection_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async with _client_with(handler) as client:
        with pytest.raises(CompaniesHouseUnavailable):
            await client.get_company_profile("09446231")


@pytest.mark.asyncio
async def test_unhandled_4xx_is_a_plain_error_not_unavailable() -> None:
    """A 400 is our malformed request. Classifying it as "unavailable" would be
    the same mistake the circuit breaker exists to avoid: our own bug wearing
    the costume of a provider outage."""
    async with _client_with(_status_handler(400)) as client:
        with pytest.raises(CompaniesHouseError) as raised:
            await client.get_company_profile("09446231")

    assert not isinstance(raised.value, CompaniesHouseUnavailable)


@pytest.mark.asyncio
async def test_non_json_200_is_unavailable_not_a_finding() -> None:
    """A 200 that isn't JSON means the register is misbehaving — which must not
    be reportable as "this company has no data"."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with _client_with(handler) as client:
        with pytest.raises(CompaniesHouseUnavailable):
            await client.get_company_profile("09446231")


# --------------------------------------------------------------------------
# Timeout — actually fired, not merely configured
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_timeout_is_enforced_against_a_real_socket() -> None:
    """A server that accepts the connection and then never answers.

    This runs over a real loopback socket with httpx's real transport, because
    asserting that a timeout *value* was passed proves nothing about whether it
    fires. No external network is involved.
    """

    async def stall(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.read(1024)  # consume the request, then answer never
            await asyncio.sleep(2)  # comfortably longer than the client's 0.3s
        finally:
            writer.close()

    server = await asyncio.start_server(stall, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    try:
        client = CompaniesHouseClient(
            api_key="k",
            base_url=f"http://127.0.0.1:{port}",
            timeout=httpx.Timeout(0.3, connect=0.3),
        )
        async with client:
            with pytest.raises(CompaniesHouseUnavailable) as raised:
                await client.get_company_profile("09446231")
    finally:
        # Not awaiting wait_closed(): it blocks on the stalling handler, which
        # is the entire point of the handler.
        server.close()

    assert "timed out" in str(raised.value)


# --------------------------------------------------------------------------
# Parsing and input handling
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_skips_unparseable_rows_rather_than_failing() -> None:
    """One malformed row degrades the result; raising would deny the user any
    result at all. The drop is logged so it cannot happen silently."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "items": [
                    {"company_number": "09446231", "title": "MONZO BANK LIMITED"},
                    {"nonsense": True},
                ]
            },
        )

    async with _client_with(handler) as client:
        results = await client.search_companies("monzo")

    assert [r.number for r in results] == ["09446231"]


@pytest.mark.asyncio
async def test_empty_search_is_a_result_not_an_error() -> None:
    async with _client_with(lambda r: httpx.Response(200, json={"items": []})) as client:
        assert await client.search_companies("nothing here") == []


@pytest.mark.asyncio
async def test_blank_search_never_reaches_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have called the register")

    async with _client_with(handler) as client:
        with pytest.raises(InvalidCompanyQuery):
            await client.search_companies("   ")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("9446231", "09446231"),  # the leading zero a human never types
        ("09446231", "09446231"),
        (" 09446231 ", "09446231"),
        ("sc123456", "SC123456"),
        ("SC 123456", "SC123456"),
        ("oc-345678", "OC345678"),
    ],
)
def test_company_number_normalisation(raw: str, expected: str) -> None:
    assert normalise_company_number(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "abc", "123456789", "!!!"])
def test_invalid_company_numbers_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidCompanyQuery):
        normalise_company_number(raw)


@pytest.mark.asyncio
async def test_profile_lookup_normalises_before_calling() -> None:
    """`9446231` must reach the register as `/company/09446231`, or the most
    natural input a user can give 404s and reads as "no such company"."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"company_number": "09446231", "company_name": "MONZO BANK LIMITED"}
        )

    async with _client_with(handler) as client:
        profile = await client.get_company_profile("9446231")

    assert seen["path"] == "/company/09446231"
    assert profile.number == "09446231"
