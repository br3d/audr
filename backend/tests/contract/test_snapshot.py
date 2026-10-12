"""Contract tests for the keyless Snapshot Hub GraphQL adapter (AUD-513)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
import respx
from httpx import Response

from audr.providers.snapshot import SnapshotError, SnapshotProposal, SnapshotProvider

_BASE = "https://hub.snapshot.org/graphql"


def _graphql_response(proposals: list[dict]) -> dict:
    return {"data": {"proposals": proposals}}


_PROPOSAL = {
    "id": "0xabc",
    "title": "Raise the fee",
    "body": "Summary text.",
    "state": "active",
    "start": 1735689600,
    "end": 1736294400,
}


@pytest.mark.contract
async def test_list_proposals_parses_response() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_BASE).mock(return_value=Response(200, json=_graphql_response([_PROPOSAL])))
        async with SnapshotProvider() as provider:
            items = await provider.list_proposals("uniswap")

    assert items == [
        SnapshotProposal(
            id="0xabc",
            title="Raise the fee",
            summary="Summary text.",
            state="active",
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 8, tzinfo=UTC),
            link="https://snapshot.org/#/uniswap/proposal/0xabc",
        )
    ]


@pytest.mark.contract
async def test_list_proposals_sends_space_variable() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_BASE).mock(return_value=Response(200, json=_graphql_response([])))
        async with SnapshotProvider() as provider:
            await provider.list_proposals("aave.eth")

    sent = route.calls.last.request
    import json as _json

    body = _json.loads(sent.content)
    assert body["variables"]["space"] == "aave.eth"


@pytest.mark.contract
async def test_list_proposals_empty_results_returns_empty_list() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_BASE).mock(return_value=Response(200, json=_graphql_response([])))
        async with SnapshotProvider() as provider:
            items = await provider.list_proposals("uniswap")
    assert items == []


@pytest.mark.contract
async def test_list_proposals_http_error_raises_snapshot_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_BASE).mock(return_value=Response(503))
        async with SnapshotProvider() as provider:
            with pytest.raises(SnapshotError):
                await provider.list_proposals("uniswap")


@pytest.mark.contract
async def test_list_proposals_network_error_raises_snapshot_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_BASE).mock(side_effect=httpx.ConnectError("boom"))
        async with SnapshotProvider() as provider:
            with pytest.raises(SnapshotError):
                await provider.list_proposals("uniswap")


@pytest.mark.contract
async def test_list_proposals_malformed_json_raises_snapshot_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_BASE).mock(return_value=Response(200, content=b"not json"))
        async with SnapshotProvider() as provider:
            with pytest.raises(SnapshotError):
                await provider.list_proposals("uniswap")


@pytest.mark.contract
async def test_list_proposals_graphql_error_raises_snapshot_error() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_BASE).mock(
            return_value=Response(200, json={"errors": [{"message": "unknown space"}]})
        )
        async with SnapshotProvider() as provider:
            with pytest.raises(SnapshotError):
                await provider.list_proposals("not-a-space")


@pytest.mark.contract
async def test_list_proposals_skips_malformed_entries() -> None:
    with respx.mock(assert_all_called=False) as mock:
        mock.post(_BASE).mock(
            return_value=Response(
                200,
                json=_graphql_response(
                    [
                        {"id": "missing-title", "state": "active", "start": 1, "end": 2},
                        _PROPOSAL,
                    ]
                ),
            )
        )
        async with SnapshotProvider() as provider:
            items = await provider.list_proposals("uniswap")
    assert [i.id for i in items] == ["0xabc"]


@pytest.mark.contract
async def test_list_proposals_sends_honest_user_agent() -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(_BASE).mock(return_value=Response(200, json=_graphql_response([])))
        async with SnapshotProvider() as provider:
            await provider.list_proposals("uniswap")
    sent = route.calls.last.request
    user_agent = sent.headers["User-Agent"]
    assert "audr" in user_agent
    assert "http" in user_agent
