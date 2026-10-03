"""Contract tests for RpcReader.get_logs() and Transfer event parsing (AUD-307)."""

from __future__ import annotations

from decimal import Decimal

import pytest
import respx
from httpx import Response

from audr.providers.rpc_reader import (
    LOG_CHUNK_SIZE,
    TRANSFER_TOPIC,
    LogEntry,
    MalformedResponseError,
    RpcError,
    RpcReader,
    _pad_address_topic,
    _topic_to_address,
    decode_transfer_amount,
)


def _rpc_ok(result: object) -> dict:
    return {"jsonrpc": "2.0", "id": 1, "result": result}


def _transfer_log(
    *,
    tx_hash: str = "0x" + "ab" * 32,
    block_number: int = 100,
    log_index: int = 0,
    token: str = "0x" + "cc" * 20,
    from_addr: str = "0x" + "aa" * 20,
    to_addr: str = "0x" + "bb" * 20,
    amount: int = 1_000_000,
) -> dict:
    """Build a minimal eth_getLogs entry for a Transfer event."""
    return {
        "transactionHash": tx_hash.lower(),
        "blockNumber": hex(block_number),
        "logIndex": hex(log_index),
        "address": token.lower(),
        "topics": [
            TRANSFER_TOPIC,
            _pad_address_topic(from_addr),
            _pad_address_topic(to_addr),
        ],
        "data": hex(amount),
    }


@pytest.mark.contract
async def test_get_logs_returns_parsed_entries() -> None:
    """get_logs returns LogEntry objects parsed from the RPC response."""
    logs = [_transfer_log(block_number=200, log_index=1, amount=5_000_000)]

    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(return_value=Response(200, json=_rpc_ok(logs)))
        reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
        result = await reader.get_logs(from_block=100, to_block=200)

    assert len(result) == 1
    entry = result[0]
    assert entry.block_number == 200
    assert entry.log_index == 1
    assert entry.topics[0] == TRANSFER_TOPIC


@pytest.mark.contract
async def test_get_logs_empty_list() -> None:
    """get_logs returns an empty list when no logs match."""
    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(return_value=Response(200, json=_rpc_ok([])))
        reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
        result = await reader.get_logs(from_block=100, to_block=200)

    assert result == []


@pytest.mark.contract
async def test_get_logs_rpc_error_raises() -> None:
    """An RPC error response raises RpcError."""
    error_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {"code": -32005, "message": "range too large"},
    }
    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(return_value=Response(200, json=error_body))
        reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
        with pytest.raises(RpcError):
            await reader.get_logs(from_block=0, to_block=999_999)


@pytest.mark.contract
async def test_get_logs_non_list_result_raises() -> None:
    """A non-list result raises MalformedResponseError."""
    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(return_value=Response(200, json=_rpc_ok("unexpected")))
        reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
        with pytest.raises(MalformedResponseError):
            await reader.get_logs(from_block=0, to_block=100)


@pytest.mark.contract
async def test_get_logs_address_filter_sent_in_params() -> None:
    """get_logs sends the address list in the filter params."""
    captured: list[dict] = []

    def capture(request, *args, **kwargs):  # type: ignore[no-untyped-def]
        captured.append(request.content.decode())
        return Response(200, json=_rpc_ok([]))

    with respx.mock() as mock:
        mock.post("http://rpc.test/").mock(side_effect=capture)
        reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
        await reader.get_logs(
            from_block=1,
            to_block=10,
            address=["0x" + "AA" * 20],
        )

    assert captured
    import json

    body = json.loads(captured[0])
    assert body["method"] == "eth_getLogs"
    params = body["params"][0]
    assert params["address"] == ["0x" + "aa" * 20]


# ---------------------------------------------------------------------------
# decode_transfer_amount unit tests (no network)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_decode_transfer_amount_basic() -> None:
    log = LogEntry(
        tx_hash="0x" + "ab" * 32,
        block_number=1,
        log_index=0,
        address="0x" + "cc" * 20,
        topics=[TRANSFER_TOPIC, "0x" + "0" * 64, "0x" + "0" * 64],
        data=hex(1_000_000_000_000_000_000),
    )
    assert decode_transfer_amount(log) == Decimal(10**18)


@pytest.mark.unit
def test_decode_transfer_amount_zero() -> None:
    log = LogEntry(
        tx_hash="0x" + "ab" * 32,
        block_number=1,
        log_index=0,
        address="0x" + "cc" * 20,
        topics=[],
        data="0x",
    )
    assert decode_transfer_amount(log) == Decimal(0)


@pytest.mark.unit
def test_decode_transfer_amount_empty() -> None:
    log = LogEntry(
        tx_hash="0x" + "ab" * 32,
        block_number=1,
        log_index=0,
        address="0x" + "cc" * 20,
        topics=[],
        data="",
    )
    assert decode_transfer_amount(log) == Decimal(0)


# ---------------------------------------------------------------------------
# _pad_address_topic and _topic_to_address round-trip
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pad_and_recover_address() -> None:
    addr = "0x" + "ab" * 20
    topic = _pad_address_topic(addr)
    assert topic.startswith("0x")
    assert len(topic) == 66  # "0x" + 64 hex chars
    recovered = _topic_to_address(topic)
    assert recovered == addr.lower()


@pytest.mark.unit
def test_log_chunk_size_is_2000() -> None:
    assert LOG_CHUNK_SIZE == 2_000
