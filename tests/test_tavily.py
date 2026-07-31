"""Direct tests for `TavilyClient` (Phase 2.2).

Tavily is the only evidence source outside the register, and the only one whose
failure is invisible by default: running out of credits produces no error a user
sees, just reports with no web evidence and honest-looking LOW confidence. So
these tests care as much about *which* failure was recorded as about whether one
was raised — the quota tracker's state is what `/health` shows, and it is the
only thing that distinguishes a broken system from a thin company.
"""

from __future__ import annotations

import httpx
import pytest

from src.data_sources.search_quota import SearchQuotaStatus
from src.data_sources.search_quota import tracker as quota
from src.data_sources.tavily import (
    SearchDepth,
    TavilyAuthError,
    TavilyClient,
    TavilyError,
    TavilyQuotaExhausted,
    TavilyUnavailable,
)

RESULTS = {
    "results": [
        {
            "url": "https://monzo.com/about/",
            "title": "About Monzo",
            "content": "Monzo is a UK digital bank offering current accounts.",
            "score": 0.94,
        },
        {
            "url": "https://www.fca.org.uk/news/example",
            "title": "FCA notice",
            "content": "Regulatory notice concerning the firm.",
            "score": 0.71,
        },
    ]
}


def _client_with(handler) -> TavilyClient:
    return TavilyClient(
        api_key="test-key",
        client=httpx.AsyncClient(
            base_url="https://tavily.test", transport=httpx.MockTransport(handler)
        ),
    )


def _responds(status: int, payload: object = None, headers: dict[str, str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload if payload is not None else {}, headers=headers)

    return handler


# --------------------------------------------------------------------------
# The request itself
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_is_a_bearer_token_and_raw_content_is_never_requested() -> None:
    """Raw page content is deliberately off: it multiplies cost for text Phase 4
    discards, and the domains most likely to matter block direct fetches
    anyway (BUILD_LOG, Phase 0)."""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=RESULTS)

    async with _client_with(handler) as client:
        await client.search("monzo business model")

    assert seen["auth"] == "Bearer test-key"
    assert '"include_raw_content":false' in str(seen["body"])


@pytest.mark.asyncio
async def test_depth_is_sent_and_charged_correctly() -> None:
    """Depth is a budget decision, not a tuning knob — advanced costs double."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.content.decode())
        return httpx.Response(200, json=RESULTS)

    async with _client_with(handler) as client:
        basic = await client.search("q", depth=SearchDepth.BASIC)
        advanced = await client.search("q", depth=SearchDepth.ADVANCED)

    assert basic.credits_spent == 1
    assert advanced.credits_spent == 2
    assert '"search_depth":"basic"' in seen[0]
    assert '"search_depth":"advanced"' in seen[1]


@pytest.mark.asyncio
async def test_empty_query_never_reaches_the_api() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not have called Tavily")

    async with _client_with(handler) as client:
        with pytest.raises(TavilyError):
            await client.search("   ")


# --------------------------------------------------------------------------
# Credit accounting — the number the report budget is derived from
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_credits_are_counted_not_just_searches() -> None:
    """Counting searches alone under-reports spend by exactly the factor the
    global report budget was sized against — which is how the first version of
    that budget was wrong."""
    async with _client_with(_responds(200, RESULTS)) as client:
        await client.search("q", depth=SearchDepth.ADVANCED)
        await client.search("q", depth=SearchDepth.ADVANCED)
        await client.search("q", depth=SearchDepth.BASIC)

    snapshot = quota.snapshot()
    assert snapshot.searches_this_container == 3
    assert snapshot.credits_this_container == 5
    assert snapshot.status is SearchQuotaStatus.OK


# --------------------------------------------------------------------------
# Failure classification
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_key_is_not_absorbed_into_quota_exhaustion(status: int) -> None:
    """A bad key must fail loudly. Recorded as exhaustion it would read as an
    expected end-of-month condition and never get fixed."""
    async with _client_with(_responds(status)) as client:
        with pytest.raises(TavilyAuthError):
            await client.search("q")

    assert quota.snapshot().status is not SearchQuotaStatus.EXHAUSTED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [402, 429])
async def test_quota_statuses_are_recorded_as_exhausted(status: int) -> None:
    """Both are treated as exhaustion. 429 is genuinely ambiguous — it can be
    short-term rate limiting — and over-reporting a stated gap is the safe
    direction, since the opposite error hides evidence loss."""
    async with _client_with(_responds(status)) as client:
        with pytest.raises(TavilyQuotaExhausted):
            await client.search("q")

    assert quota.snapshot().status is SearchQuotaStatus.EXHAUSTED


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_unavailable_not_exhausted(status: int) -> None:
    """An outage may resolve; exhaustion will not. Phase 2.4 responds
    differently to each, so they must not be collapsed."""
    async with _client_with(_responds(status)) as client:
        with pytest.raises(TavilyUnavailable):
            await client.search("q")

    assert quota.snapshot().status is SearchQuotaStatus.ERROR


@pytest.mark.asyncio
async def test_malformed_request_is_our_error_not_an_outage() -> None:
    async with _client_with(_responds(400)) as client:
        with pytest.raises(TavilyError) as raised:
            await client.search("q")

    assert not isinstance(raised.value, TavilyUnavailable)


@pytest.mark.asyncio
async def test_connection_error_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    async with _client_with(handler) as client:
        with pytest.raises(TavilyUnavailable):
            await client.search("q")


@pytest.mark.asyncio
async def test_non_json_200_is_unavailable_not_an_empty_result() -> None:
    """An empty result list would be reportable as "nothing was written about
    this company" — which is a claim, and a false one."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>maintenance</html>")

    async with _client_with(handler) as client:
        with pytest.raises(TavilyUnavailable):
            await client.search("q")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_results_are_parsed_and_timestamped_at_retrieval() -> None:
    """Phase 2.3 requires a retrieval timestamp per document. Stamped here, not
    at render time — otherwise it describes the report run, not the evidence."""
    async with _client_with(_responds(200, RESULTS)) as client:
        response = await client.search("monzo")

    assert [r.url for r in response.results] == [
        "https://monzo.com/about/",
        "https://www.fca.org.uk/news/example",
    ]
    assert response.results[0].retrieved_at.tzinfo is not None


@pytest.mark.asyncio
async def test_one_unparseable_result_does_not_sink_the_rest() -> None:
    payload = {"results": [{"no_url_field": True}, RESULTS["results"][0]]}
    async with _client_with(_responds(200, payload)) as client:
        response = await client.search("monzo")

    assert [r.url for r in response.results] == ["https://monzo.com/about/"]


@pytest.mark.asyncio
async def test_no_results_is_a_result_not_an_error() -> None:
    async with _client_with(_responds(200, {"results": []})) as client:
        response = await client.search("a company nobody wrote about")

    assert response.results == []
    assert response.credits_spent == 1  # a search that found nothing still costs
