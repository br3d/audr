"""Ethereum JSON-RPC reader: chain-ID validation, eth_getBalance, ERC-20 balanceOf (T030 / US1).

No signing methods are exposed — read-only operations only.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

# balanceOf(address) selector
_BALANCE_OF_SELECTOR = "0x70a08231"
_HEX_PREFIX = "0x"


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

    async def get_eth_balance(self, address: str) -> int:
        """Return the ETH balance of *address* in Wei as an integer."""
        checksum = _normalise_address(address)
        result = await self._call("eth_getBalance", [checksum, "latest"])
        return _parse_hex_int(result)

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
