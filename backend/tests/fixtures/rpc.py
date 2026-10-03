"""Controlled Ethereum JSON-RPC HTTP fixtures for contract and integration tests (T015)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import respx
from httpx import Response


def _rpc_response(result: Any, *, id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def _rpc_error(code: int, message: str, *, id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


@pytest.fixture()
def rpc_mock() -> Iterator[respx.MockRouter]:
    """HTTPX mock router pre-configured with default Ethereum RPC responses."""
    with respx.mock(assert_all_called=False) as mock:
        # Default: return chain ID 1 (mainnet)
        mock.post("http://rpc.test/").mock(return_value=Response(200, json=_rpc_response("0x1")))
        yield mock


@pytest.fixture()
def rpc_url() -> str:
    return "http://rpc.test/"


class EthRpcStub:
    """Programmatic Ethereum RPC stub for fine-grained test control."""

    def __init__(self, router: respx.MockRouter, url: str = "http://rpc.test/") -> None:
        self._router = router
        self._url = url

    def set_balance(self, address: str, wei: int) -> None:
        hex_balance = hex(wei)
        self._router.post(self._url).mock(
            return_value=Response(200, json=_rpc_response(hex_balance))
        )

    def set_chain_id(self, chain_id: int) -> None:
        self._router.post(self._url).mock(
            return_value=Response(200, json=_rpc_response(hex(chain_id)))
        )

    def set_error(self, code: int, message: str) -> None:
        self._router.post(self._url).mock(
            return_value=Response(200, json=_rpc_error(code, message))
        )

    def set_http_error(self, status_code: int) -> None:
        self._router.post(self._url).mock(return_value=Response(status_code))
