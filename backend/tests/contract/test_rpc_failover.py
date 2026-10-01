"""Contract tests: RpcReader fails over to the keyless public endpoints when the
configured provider reports itself unusable — HTTP 402 Payment Required being
the case that took staging down (AUD-364)."""

from __future__ import annotations

import pytest
import respx
from httpx import ConnectError, Response

from audr.jobs.policy import RetryPolicy
from audr.providers.rpc_defaults import DEFAULT_PUBLIC_RPC_URLS
from audr.providers.rpc_reader import RpcError, RpcReader

PRIMARY = "http://primary.test/"
FALLBACK = "http://fallback.test/"


def _rpc_ok(result: object) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _reader(**kwargs: object) -> RpcReader:
    return RpcReader(
        url=PRIMARY,
        fallback_urls=[FALLBACK],
        expected_chain_id=1,
        retry_policy=RetryPolicy(max_attempts=1, base_delay_s=0.0, jitter=False),
        **kwargs,  # type: ignore[arg-type]
    )


@pytest.mark.contract
@pytest.mark.parametrize("status", [401, 402, 403, 500, 503])
async def test_unusable_endpoint_fails_over(status: int) -> None:
    """A provider-level failure on the primary is served by the fallback instead
    of failing the job."""
    with respx.mock() as mock:
        mock.post(PRIMARY).mock(return_value=Response(status))
        mock.post(FALLBACK).mock(return_value=Response(200, json=_rpc_ok("0x2a")))
        assert await _reader().get_block_number() == 42


@pytest.mark.contract
async def test_transport_error_fails_over() -> None:
    """An endpoint we cannot even reach is treated the same as one that refuses us."""
    with respx.mock() as mock:
        mock.post(PRIMARY).mock(side_effect=ConnectError("no route"))
        mock.post(FALLBACK).mock(return_value=Response(200, json=_rpc_ok("0x1")))
        assert await _reader().get_block_number() == 1


@pytest.mark.contract
async def test_429_exhausts_retries_then_fails_over() -> None:
    """429 is still retried against the same endpoint first (AUD-362); only once
    those retries are spent do we move on."""
    primary_calls = 0

    def _throttle(request):  # type: ignore[no-untyped-def]
        nonlocal primary_calls
        primary_calls += 1
        return Response(429, headers={"Retry-After": "0"})

    with respx.mock() as mock:
        mock.post(PRIMARY).mock(side_effect=_throttle)
        mock.post(FALLBACK).mock(return_value=Response(200, json=_rpc_ok("0x3")))
        reader = RpcReader(
            url=PRIMARY,
            fallback_urls=[FALLBACK],
            expected_chain_id=1,
            retry_policy=RetryPolicy(max_attempts=2, base_delay_s=0.0, jitter=False),
        )
        assert await reader.get_block_number() == 3

    assert primary_calls == 3  # initial attempt + 2 retries


@pytest.mark.contract
async def test_working_endpoint_is_sticky() -> None:
    """After failing over, later calls in the same run go straight to the endpoint
    that answered — the dead primary is not re-probed per call."""
    primary_calls = 0

    def _dead(request):  # type: ignore[no-untyped-def]
        nonlocal primary_calls
        primary_calls += 1
        return Response(402)

    with respx.mock() as mock:
        mock.post(PRIMARY).mock(side_effect=_dead)
        mock.post(FALLBACK).mock(return_value=Response(200, json=_rpc_ok("0x1")))
        reader = _reader()
        await reader.get_block_number()
        await reader.get_block_number()
        await reader.get_block_number()

    assert primary_calls == 1


@pytest.mark.contract
async def test_jsonrpc_error_does_not_fail_over() -> None:
    """A JSON-RPC-level error is a property of the request, so it would fail
    identically everywhere — raise instead of burning the fallback budget."""
    fallback_calls = 0

    def _count(request):  # type: ignore[no-untyped-def]
        nonlocal fallback_calls
        fallback_calls += 1
        return Response(200, json=_rpc_ok("0x1"))

    # assert_all_called=False: the fallback route is deliberately never hit —
    # that is the point of the test.
    with respx.mock(assert_all_called=False) as mock:
        mock.post(PRIMARY).mock(
            return_value=Response(
                200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nope"}}
            )
        )
        mock.post(FALLBACK).mock(side_effect=_count)
        with pytest.raises(RpcError, match="-32000"):
            await _reader().get_block_number()

    assert fallback_calls == 0


@pytest.mark.contract
async def test_all_endpoints_unusable_raises() -> None:
    """When nothing is usable the job still fails, naming the last cause."""
    with respx.mock() as mock:
        mock.post(PRIMARY).mock(return_value=Response(402))
        mock.post(FALLBACK).mock(return_value=Response(402))
        with pytest.raises(RpcError, match="all 2 RPC endpoint"):
            await _reader().get_block_number()


@pytest.mark.contract
async def test_duplicate_primary_not_retried_as_fallback() -> None:
    """A configured URL that is already one of the public defaults is not tried twice."""
    url = DEFAULT_PUBLIC_RPC_URLS[0]
    reader = RpcReader(url=url, fallback_urls=DEFAULT_PUBLIC_RPC_URLS, expected_chain_id=1)
    assert reader._urls.count(url) == 1
    assert len(reader._urls) == len(DEFAULT_PUBLIC_RPC_URLS)
