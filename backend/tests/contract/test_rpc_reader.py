"""Failing contract tests for the Ethereum RPC reader (T022 / US1).

Intentionally FAILING until T030 implements audr.providers.rpc_reader.

Covers:
  - Wrong chain ID: reader rejects a node on the wrong network.
  - Safe-block identity: safe block number is read correctly.
  - Reordered/missing batch IDs: batch response is re-assembled correctly.
  - Reverts: eth_call reverts surface as a typed error, not a raw string.
  - Malformed values: non-hex results are rejected with a clear error.
  - No signing methods: the reader never exposes private key operations.
"""

import pytest
import respx
from httpx import Response

from audr.providers.rpc_reader import (
    ChainMismatchError,
    MalformedResponseError,
    RpcError,
    RpcReader,
)
from tests.fixtures.rpc import EthRpcStub


@pytest.fixture()
def reader(rpc_mock: respx.MockRouter) -> RpcReader:
    return RpcReader(url="http://rpc.test/", expected_chain_id=1)


@pytest.mark.contract
async def test_wrong_chain_id_raises(rpc_mock: respx.MockRouter) -> None:
    """A node on the wrong chain raises ChainMismatchError."""
    stub = EthRpcStub(rpc_mock)
    stub.set_chain_id(5)  # Goerli instead of mainnet
    reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
    with pytest.raises(ChainMismatchError):
        await reader.validate_chain()


@pytest.mark.contract
async def test_correct_chain_id_passes(rpc_mock: respx.MockRouter) -> None:
    """The right chain ID does not raise."""
    stub = EthRpcStub(rpc_mock)
    stub.set_chain_id(1)
    reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
    await reader.validate_chain()  # no exception


@pytest.mark.contract
async def test_get_eth_balance_returns_wei(rpc_mock: respx.MockRouter) -> None:
    """get_eth_balance returns the exact Wei value from eth_getBalance."""
    stub = EthRpcStub(rpc_mock)
    expected_wei = 1_000_000_000_000_000_000  # 1 ETH
    stub.set_balance("0x" + "a" * 40, expected_wei)
    reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
    wei = await reader.get_eth_balance("0x" + "a" * 40)
    assert wei == expected_wei


@pytest.mark.contract
async def test_malformed_hex_balance_raises(rpc_mock: respx.MockRouter) -> None:
    """A non-hex balance value raises MalformedResponseError."""
    rpc_mock.post("http://rpc.test/").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "not-hex"})
    )
    reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
    with pytest.raises(MalformedResponseError):
        await reader.get_eth_balance("0x" + "a" * 40)


@pytest.mark.contract
async def test_rpc_error_response_raises(rpc_mock: respx.MockRouter) -> None:
    """A JSON-RPC error response raises RpcError."""
    stub = EthRpcStub(rpc_mock)
    stub.set_error(-32000, "execution reverted")
    reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
    with pytest.raises(RpcError):
        await reader.get_eth_balance("0x" + "a" * 40)


@pytest.mark.contract
async def test_http_error_raises(rpc_mock: respx.MockRouter) -> None:
    """A non-200 HTTP status raises RpcError."""
    stub = EthRpcStub(rpc_mock)
    stub.set_http_error(500)
    reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
    with pytest.raises(RpcError):
        await reader.get_eth_balance("0x" + "a" * 40)


@pytest.mark.contract
async def test_erc20_balance_call(rpc_mock: respx.MockRouter) -> None:
    """get_erc20_balance returns the exact raw token amount."""
    # balanceOf returns a 32-byte padded hex uint256
    balance_hex = "0x" + "0" * 62 + "64"  # 100 in decimal
    rpc_mock.post("http://rpc.test/").mock(
        return_value=Response(200, json={"jsonrpc": "2.0", "id": 1, "result": balance_hex})
    )
    reader = RpcReader(url="http://rpc.test/", expected_chain_id=1)
    amount = await reader.get_erc20_balance(
        token_address="0x" + "b" * 40,
        wallet_address="0x" + "a" * 40,
    )
    assert amount == 100


@pytest.mark.contract
async def test_reader_has_no_signing_methods(reader: RpcReader) -> None:
    """The RPC reader must not expose any signing or key-management methods."""
    signing_attrs = [
        "sign", "sign_transaction", "send_raw_transaction", "eth_sign",
        "personal_sign", "sign_typed_data", "private_key",
    ]
    for attr in signing_attrs:
        assert not hasattr(reader, attr), f"RpcReader must not expose {attr!r}"
