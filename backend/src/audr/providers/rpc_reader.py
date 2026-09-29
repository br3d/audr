"""Ethereum JSON-RPC reader: chain-ID validation, eth_getBalance, ERC-20 balanceOf,
eth_getLogs (AUD-307, AUD-300).

No signing methods are exposed — read-only operations only.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# balanceOf(address) selector
_BALANCE_OF_SELECTOR = "0x70a08231"
_HEX_PREFIX = "0x"

# Transfer(address,address,uint256) keccak256 topic
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Approval(address,address,uint256) keccak256 topic
APPROVAL_TOPIC = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"

# Maximum block range per eth_getLogs call; avoids RPC timeout on large ranges
LOG_CHUNK_SIZE = 2_000


@dataclass
class LogEntry:
    """A single decoded log entry from eth_getLogs."""

    tx_hash: str
    block_number: int
    log_index: int
    address: str  # contract that emitted the log (lowercase)
    topics: list[str]
    data: str  # raw hex data field


class ChainMismatchError(Exception):
    """Raised when the node's chain ID differs from the expected value."""


class RpcError(Exception):
    """Raised for JSON-RPC error responses or non-200 HTTP status codes."""


class MalformedResponseError(Exception):
    """Raised when the RPC result cannot be parsed as expected."""


class RpcReader:
    """Read-only Ethereum JSON-RPC client.

    Only eth_chainId, eth_blockNumber, eth_getBalance, and eth_call (balanceOf)
    are supported.  No signing, key management, or write operations.
    """

    def __init__(
        self,
        *,
        url: str,
        expected_chain_id: int,
        timeout: float = 10.0,
    ) -> None:
        self._url = url
        self._expected_chain_id = expected_chain_id
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            follow_redirects=False,  # never follow redirects — DNS rebinding risk
        )

    async def validate_chain(self) -> None:
        """Confirm the node is on the expected chain.  Raises ChainMismatchError."""
        result = await self._call("eth_chainId", [])
        chain_id = _parse_hex_int(result)
        if chain_id != self._expected_chain_id:
            raise ChainMismatchError(
                f"node chain ID {chain_id} != expected {self._expected_chain_id}"
            )

    async def get_block_number(self) -> int:
        """Return the current block number."""
        result = await self._call("eth_blockNumber", [])
        return _parse_hex_int(result)

    async def get_eth_balance(self, address: str) -> int:
        """Return the ETH balance of *address* in Wei as an integer."""
        checksum = _normalise_address(address)
        result = await self._call("eth_getBalance", [checksum, "latest"])
        return _parse_hex_int(result)

    async def eth_call(
        self,
        *,
        to: str,
        data: str,
        block: str = "latest",
    ) -> str:
        """Generic read-only eth_call; returns the raw hex result string."""
        return await self._call("eth_call", [{"to": to, "data": data}, block])

    async def get_erc20_balance(
        self,
        *,
        token_address: str,
        wallet_address: str,
    ) -> int:
        """Return the ERC-20 token balance for *wallet_address* as raw integer units."""
        token = _normalise_address(token_address)
        wallet = _normalise_address(wallet_address)
        # balanceOf(address) — pad wallet to 32 bytes
        padded = wallet[2:].lower().zfill(64)
        data = _BALANCE_OF_SELECTOR + padded
        result = await self._call("eth_call", [{"to": token, "data": data}, "latest"])
        return _parse_hex_int(result)

    async def get_logs(
        self,
        *,
        from_block: int,
        to_block: int,
        address: list[str] | None = None,
        topics: list[str | list[str] | None] | None = None,
    ) -> list[LogEntry]:
        """Fetch logs via eth_getLogs for a single block range (no chunking).

        The caller is responsible for chunking long ranges using LOG_CHUNK_SIZE.
        ``address`` is a list of contract addresses to filter by (OR logic).
        ``topics`` follows the eth_getLogs topic filter spec.
        """
        filter_params: dict[str, Any] = {
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        if address:
            filter_params["address"] = [_normalise_address(a) for a in address]
        if topics is not None:
            filter_params["topics"] = topics

        raw = await self._call_raw("eth_getLogs", [filter_params])
        if not isinstance(raw, list):
            raise MalformedResponseError(
                f"eth_getLogs expected list, got {type(raw).__name__}"
            )
        return [_parse_log_entry(item) for item in raw]

    async def _call_raw(self, method: str, params: list) -> Any:  # type: ignore[type-arg]
        """Like _call but returns the parsed Python value instead of str."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise RpcError(f"HTTP error calling {method}") from exc

        if resp.status_code != 200:
            raise RpcError(f"HTTP {resp.status_code} from RPC endpoint")

        try:
            body = resp.json()
        except Exception as exc:
            raise MalformedResponseError("RPC response is not valid JSON") from exc

        if "error" in body:
            err = body["error"]
            raise RpcError(f"RPC error {err.get('code')}: {err.get('message')}")

        result = body.get("result")
        if result is None:
            raise MalformedResponseError("RPC response has no 'result' field")
        return result

    async def _call(self, method: str, params: list) -> str:  # type: ignore[type-arg]
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            raise RpcError(f"HTTP error calling {method}") from exc

        if resp.status_code != 200:
            raise RpcError(f"HTTP {resp.status_code} from RPC endpoint")

        try:
            body = resp.json()
        except Exception as exc:
            raise MalformedResponseError("RPC response is not valid JSON") from exc

        if "error" in body:
            err = body["error"]
            raise RpcError(f"RPC error {err.get('code')}: {err.get('message')}")

        result = body.get("result")
        if result is None:
            raise MalformedResponseError("RPC response has no 'result' field")
        return str(result)

    async def __aenter__(self) -> RpcReader:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self._client.aclose()


def _parse_hex_int(value: str) -> int:
    if not isinstance(value, str) or not value.startswith(_HEX_PREFIX):
        raise MalformedResponseError(f"expected hex string, got {value!r}")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise MalformedResponseError(f"cannot parse hex value {value!r}") from exc


def _normalise_address(address: str) -> str:
    """Return lowercase hex address with 0x prefix."""
    return address.lower() if address.startswith("0x") else "0x" + address.lower()


def _pad_address_topic(address: str) -> str:
    """Pad a 20-byte address to a 32-byte topic (left-zero-padded, lowercase with 0x)."""
    addr = _normalise_address(address)
    return "0x" + addr[2:].zfill(64)


def _parse_log_entry(item: Any) -> LogEntry:
    if not isinstance(item, dict):
        raise MalformedResponseError(f"log entry is not a dict: {item!r}")
    try:
        return LogEntry(
            tx_hash=item["transactionHash"],
            block_number=_parse_hex_int(item["blockNumber"]),
            log_index=_parse_hex_int(item["logIndex"]),
            address=item["address"].lower(),
            topics=[t.lower() for t in item.get("topics", [])],
            data=item.get("data", "0x"),
        )
    except (KeyError, TypeError) as exc:
        raise MalformedResponseError(f"malformed log entry: {exc}") from exc


def _topic_to_address(topic: str) -> str:
    """Extract a 20-byte lowercase address from a 32-byte topic (last 20 bytes)."""
    return "0x" + topic[-40:].lower()


def decode_transfer_amount(log: LogEntry) -> Decimal:
    """Decode the uint256 amount from a Transfer event's data field."""
    data = log.data
    if not data or data == "0x":
        return Decimal(0)
    if not data.startswith("0x"):
        raise MalformedResponseError(f"Transfer data not hex: {data!r}")
    try:
        return Decimal(int(data, 16))
    except ValueError as exc:
        raise MalformedResponseError(f"cannot parse Transfer amount: {data!r}") from exc
