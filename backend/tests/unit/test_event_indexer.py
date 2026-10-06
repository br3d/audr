"""Unit tests for event_indexer helper functions (AUD-307)."""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from audr.jobs.event_indexer import _insert_event
from audr.providers.rpc_reader import (
    APPROVAL_TOPIC,
    TRANSFER_TOPIC,
    LogEntry,
    _pad_address_topic,
    _topic_to_address,
    decode_transfer_amount,
)


@pytest.mark.unit
class TestTopicHelpers:
    def test_topic_to_address_roundtrip(self) -> None:
        addr = "0x" + "de" * 20
        topic = _pad_address_topic(addr)
        assert _topic_to_address(topic) == addr.lower()

    def test_topic_length(self) -> None:
        addr = "0xabcdef1234567890abcdef1234567890abcdef12"
        topic = _pad_address_topic(addr)
        # "0x" + 64 hex chars = 66
        assert len(topic) == 66

    def test_topic_leading_zeros(self) -> None:
        """Address occupies last 40 chars; first 24 chars are zeros."""
        addr = "0x" + "ab" * 20
        topic = _pad_address_topic(addr)
        assert topic[2:26] == "0" * 24

    def test_topic_to_address_lowercase(self) -> None:
        addr = "0x" + "AB" * 20
        topic = _pad_address_topic(addr)
        recovered = _topic_to_address(topic)
        assert recovered == "0x" + "ab" * 20


@pytest.mark.unit
class TestDecodeTransferAmount:
    def test_standard_erc20_amount(self) -> None:
        # 1 USDC = 1_000_000 (6 decimals)
        log = _make_log(data=hex(1_000_000))
        assert decode_transfer_amount(log) == Decimal(1_000_000)

    def test_large_uint256(self) -> None:
        max_uint = 2**256 - 1
        log = _make_log(data=hex(max_uint))
        assert decode_transfer_amount(log) == Decimal(max_uint)

    def test_empty_data(self) -> None:
        log = _make_log(data="")
        assert decode_transfer_amount(log) == Decimal(0)

    def test_0x_data(self) -> None:
        log = _make_log(data="0x")
        assert decode_transfer_amount(log) == Decimal(0)

    def test_zero_amount(self) -> None:
        log = _make_log(data="0x0")
        assert decode_transfer_amount(log) == Decimal(0)


@pytest.mark.unit
class TestApprovalTopic:
    def test_distinct_from_transfer_topic(self) -> None:
        assert APPROVAL_TOPIC != TRANSFER_TOPIC

    def test_topic_format(self) -> None:
        assert APPROVAL_TOPIC.startswith("0x")
        assert len(APPROVAL_TOPIC) == 66
        assert APPROVAL_TOPIC == APPROVAL_TOPIC.lower()


@pytest.mark.unit
class TestInsertEventApprovalClassification:
    """_insert_event must classify Approval logs as event_type='approval',
    owner->from_address, spender->to_address, and skip logs where the wallet
    is not the owner (AUD-300)."""

    async def test_approval_owned_by_wallet_is_inserted(self) -> None:
        session = _make_session_mock()
        wallet_address = "0x" + "11" * 20
        spender = "0x" + "22" * 20
        log = _make_log(
            topics=[
                APPROVAL_TOPIC,
                _pad_address_topic(wallet_address),
                _pad_address_topic(spender),
            ],
            data=hex(2**256 - 1),
        )

        n = await _insert_event(
            session, log=log, wallet_id=_WALLET_ID, wallet_address=wallet_address
        )

        assert n == 1
        params = session.execute.call_args.args[1]
        assert params["event_type"] == "approval"
        assert params["from_address"] == wallet_address
        assert params["to_address"] == spender
        assert params["raw_amount"] == str(2**256 - 1)

    async def test_approval_not_owned_by_wallet_is_skipped(self) -> None:
        """Defensive: if the RPC filter ever returns a log where the wallet is
        not the owner, it must not be recorded as that wallet's approval."""
        session = _make_session_mock()
        wallet_address = "0x" + "11" * 20
        other_owner = "0x" + "33" * 20
        spender = "0x" + "22" * 20
        log = _make_log(
            topics=[
                APPROVAL_TOPIC,
                _pad_address_topic(other_owner),
                _pad_address_topic(spender),
            ],
            data=hex(1_000),
        )

        n = await _insert_event(
            session, log=log, wallet_id=_WALLET_ID, wallet_address=wallet_address
        )

        assert n == 0
        session.execute.assert_not_called()

    async def test_erc721_approval_is_skipped(self) -> None:
        """ERC-721 shares the Approval signature but indexes tokenId, so it
        arrives with four topics and empty data. Since the approval log filter
        is not restricted to the tracked ERC-20 catalog (AUD-445), an NFT
        approval must not be recorded as a zero-value token allowance."""
        session = _make_session_mock()
        wallet_address = "0x" + "11" * 20
        operator = "0x" + "22" * 20
        log = _make_log(
            topics=[
                APPROVAL_TOPIC,
                _pad_address_topic(wallet_address),
                _pad_address_topic(operator),
                "0x" + f"{7:064x}",  # tokenId, indexed
            ],
            data="0x",
        )

        n = await _insert_event(
            session, log=log, wallet_id=_WALLET_ID, wallet_address=wallet_address
        )

        assert n == 0
        session.execute.assert_not_called()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WALLET_ID = uuid.uuid4()


def _make_session_mock() -> AsyncMock:
    session = AsyncMock()
    result = MagicMock()
    result.rowcount = 1
    session.execute.return_value = result
    return session


def _make_log(**kwargs: str) -> LogEntry:
    defaults: dict = {
        "tx_hash": "0x" + "aa" * 32,
        "block_number": 1,
        "log_index": 0,
        "address": "0x" + "cc" * 20,
        "topics": [TRANSFER_TOPIC, "0x" + "0" * 64, "0x" + "0" * 64],
        "data": "0x0",
    }
    defaults.update(kwargs)
    return LogEntry(**defaults)
