"""Unit tests for Snapshot GraphQL response -> proposal mapping (AUD-513)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from audr.jobs.news import _snapshot_title
from audr.providers.snapshot import SnapshotProposal, _parse_proposal

_ENTRY = {
    "id": "0xabc123",
    "title": "Deploy v4 fee switch",
    "body": "This proposal activates the protocol fee switch.",
    "state": "active",
    "start": 1735689600,
    "end": 1736294400,
}


@pytest.mark.unit
class TestParseProposal:
    def test_maps_fields_into_proposal(self) -> None:
        proposal = _parse_proposal(_ENTRY, space_id="uniswap")
        assert proposal == SnapshotProposal(
            id="0xabc123",
            title="Deploy v4 fee switch",
            summary="This proposal activates the protocol fee switch.",
            state="active",
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 8, tzinfo=UTC),
            link="https://snapshot.org/#/uniswap/proposal/0xabc123",
        )

    def test_truncates_long_body_to_summary(self) -> None:
        entry = {**_ENTRY, "body": "x" * 500}
        proposal = _parse_proposal(entry, space_id="uniswap")
        assert proposal is not None
        assert len(proposal.summary) == 280
        assert proposal.summary.endswith("…")

    def test_missing_body_yields_empty_summary(self) -> None:
        entry = {k: v for k, v in _ENTRY.items() if k != "body"}
        proposal = _parse_proposal(entry, space_id="uniswap")
        assert proposal is not None
        assert proposal.summary == ""

    def test_missing_required_field_returns_none(self) -> None:
        for field in ("id", "title", "state"):
            entry = {k: v for k, v in _ENTRY.items() if k != field}
            assert _parse_proposal(entry, space_id="uniswap") is None

    def test_unparseable_start_returns_none(self) -> None:
        entry = {**_ENTRY, "start": "not-a-timestamp"}
        assert _parse_proposal(entry, space_id="uniswap") is None


@pytest.mark.unit
class TestSnapshotTitle:
    def test_folds_state_into_title(self) -> None:
        proposal = SnapshotProposal(
            id="p1",
            title="Deploy v4 fee switch",
            summary="",
            state="active",
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 8, tzinfo=UTC),
            link="https://snapshot.org/#/uniswap/proposal/p1",
        )
        assert _snapshot_title(proposal) == "[active] Deploy v4 fee switch"

    def test_title_changes_when_state_changes(self) -> None:
        proposal = SnapshotProposal(
            id="p1",
            title="Deploy v4 fee switch",
            summary="",
            state="closed",
            start=datetime(2025, 1, 1, tzinfo=UTC),
            end=datetime(2025, 1, 8, tzinfo=UTC),
            link="https://snapshot.org/#/uniswap/proposal/p1",
        )
        assert _snapshot_title(proposal) == "[closed] Deploy v4 fee switch"
