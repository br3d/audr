"""ERC-20 metadata reading — on-chain (untrusted) with catalog fallback (T037).

On-chain calls can lie or revert.  Results are always validated against expected
ranges and are NEVER trusted for financial calculations (only for display).
Catalog metadata wins when available.
"""

from __future__ import annotations

from dataclasses import dataclass

from audr.providers.rpc_reader import MalformedResponseError, RpcError, RpcReader

# ERC-20 function selectors
_NAME_SELECTOR = "0x06fdde03"
_SYMBOL_SELECTOR = "0x95d89b41"
_DECIMALS_SELECTOR = "0x313ce567"

_MAX_SYMBOL_LEN = 64
_MAX_NAME_LEN = 256
_MAX_DECIMALS = 77  # uint8 max is 255 but >77 is nonsensical for ERC-20


@dataclass
class Erc20Metadata:
    symbol: str
    name: str
    decimals: int
    from_catalog: bool = False


class MetadataReadError(Exception):
    """Raised when on-chain metadata cannot be retrieved or is malformed."""


async def read_erc20_metadata(
    rpc: RpcReader,
    token_address: str,
) -> Erc20Metadata:
    """Read ERC-20 name/symbol/decimals from the contract.

    All three calls are made independently; if any reverts we raise MetadataReadError.
    Callers should fall back to catalog data on MetadataReadError.
    """
    try:
        symbol_raw = await _call(rpc, token_address, _SYMBOL_SELECTOR)
        name_raw = await _call(rpc, token_address, _NAME_SELECTOR)
        decimals_raw = await _call(rpc, token_address, _DECIMALS_SELECTOR)
    except (RpcError, MalformedResponseError) as exc:
        raise MetadataReadError(str(exc)) from exc

    symbol = _decode_string(symbol_raw, "symbol", _MAX_SYMBOL_LEN)
    name = _decode_string(name_raw, "name", _MAX_NAME_LEN)
    decimals = _decode_uint8(decimals_raw)

    return Erc20Metadata(symbol=symbol, name=name, decimals=decimals)


async def _call(rpc: RpcReader, address: str, selector: str) -> str:
    """eth_call and return the hex result string."""
    return await rpc.eth_call(to=address, data=selector)


def _decode_string(hex_data: str, field: str, max_len: int) -> str:
    """ABI-decode a dynamic string return value from eth_call hex output."""
    data = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(data) < 96:  # offset (32) + length (32) + at least one word (32)
        raise MetadataReadError(f"{field}: response too short to be a valid string")
    length = int.from_bytes(data[32:64], "big")
    if length > max_len:
        raise MetadataReadError(f"{field}: string too long ({length} > {max_len})")
    raw = data[64 : 64 + length]
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise MetadataReadError(f"{field}: invalid UTF-8") from exc


def _decode_uint8(hex_data: str) -> int:
    """ABI-decode a uint8 return value (right-padded 32-byte word)."""
    data = bytes.fromhex(hex_data.removeprefix("0x"))
    if len(data) < 32:
        raise MetadataReadError("decimals: response too short")
    value = int.from_bytes(data[:32], "big")
    if value > _MAX_DECIMALS:
        raise MetadataReadError(f"decimals: out of range ({value})")
    return value
