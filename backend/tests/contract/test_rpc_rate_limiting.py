"""Contract tests: RpcReader routes every RPC call through the shared rate
limiter and retries HTTP 429 responses with backoff instead of failing the
whole call on the first throttle (AUD-362)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import respx
from httpx import Response

from audr.jobs.policy import RetryPolicy
from audr.providers.rpc_reader import RpcError, RpcReader


def _rpc_ok(result: object) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


@pytest.mark.contract
async def test_rate_limiter_acquire_called_before_request() -> None:
    """Every outgoing call must acquire a token from the shared limiter first."""
    limiter = AsyncMock()
    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(return_value=Response(200, json=_rpc_ok("0x1")))
        reader = RpcReader(url="http://rpc.test/", expected_chain_id=1, rate_limiter=limiter)
        await reader.get_block_number()

    limiter.acquire.assert_awaited_once()


@pytest.mark.contract
async def test_no_rate_limiter_means_unthrottled() -> None:
    """Passing no limiter (the default) preserves today's unthrottled behaviour."""
    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(return_value=Response(200, json=_rpc_ok("0x1")))
        reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
        block = await reader.get_block_number()

    assert block == 1


@pytest.mark.contract
async def test_429_is_retried_and_acquires_limiter_each_attempt() -> None:
    """A 429 followed by a 200 succeeds, and the limiter is consulted on each attempt."""
    limiter = AsyncMock()
    responses = iter(
        [
            Response(429, headers={"Retry-After": "0"}),
            Response(200, json=_rpc_ok("0x2a")),  # 42
        ]
    )

    def _respond(request):  # type: ignore[no-untyped-def]
        return next(responses)

    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(side_effect=_respond)
        reader = RpcReader(
            url="http://rpc.test/",
            expected_chain_id=1,
            rate_limiter=limiter,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_s=0.0, jitter=False),
        )
        block = await reader.get_block_number()

    assert block == 42
    assert limiter.acquire.await_count == 2


@pytest.mark.contract
async def test_429_retries_exhausted_raises_rpc_error() -> None:
    """Persistent 429s raise RpcError once the retry budget is exhausted, instead
    of retrying forever."""
    call_count = 0

    def _respond(request):  # type: ignore[no-untyped-def]
        nonlocal call_count
        call_count += 1
        return Response(429)

    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(side_effect=_respond)
        reader = RpcReader(
            url="http://rpc.test/",
            expected_chain_id=1,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_s=0.0, jitter=False),
        )
        with pytest.raises(RpcError):
            await reader.get_block_number()

    assert call_count == 3  # 1 initial attempt + 2 retries


@pytest.mark.contract
async def test_429_without_retry_after_uses_policy_backoff() -> None:
    """When the provider sends no Retry-After header, the call still retries
    using the configured backoff instead of giving up immediately."""
    responses = iter([Response(429), Response(200, json=_rpc_ok("0x1"))])

    def _respond(request):  # type: ignore[no-untyped-def]
        return next(responses)

    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(side_effect=_respond)
        reader = RpcReader(
            url="http://rpc.test/",
            expected_chain_id=1,
            retry_policy=RetryPolicy(max_attempts=1, base_delay_s=0.0, jitter=False),
        )
        block = await reader.get_block_number()

    assert block == 1
