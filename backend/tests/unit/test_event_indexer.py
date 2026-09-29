"""Unit tests for event_indexer helper functions (AUD-307)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from audr.providers.rpc_reader import (
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
