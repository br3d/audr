"""Integration tests for portfolio data exports (audr.operations.exports).

These tests are intentionally failing until audr/operations/exports.py is created.

Covers:
  - JSON schema_version = 1 present at the top level of all JSON exports
  - CSV # schema_version: 1 metadata comment in the header
  - record_type field distinguishes "current_portfolio" from "full_history"
  - Exact Decimal preservation: raw_amount serialised as a string, never float
  - Unknown-vs-zero: a wallet with no balance observation renders as null (JSON)
    or empty string (CSV), never as 0
  - Excluded assets (excluded=True) are absent from the current-portfolio export
  - Historical asset names are sourced from asset_metadata_revision at snapshot time
  - Full-history export includes all published snapshots ordered by snapshotted_at
    ascending
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.exports import (
    export_current_portfolio,
    export_full_history,
    render_portfolio_csv,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Helpers — data insertion
# ---------------------------------------------------------------------------


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wid = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO wallet (id, address, label, status)"
            " VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wid), "addr": address.lower()},
    )
    return wid


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str,
    symbol: str = "TKN",
    name: str = "Token",
    decimals: int = 18,
    excluded: bool = False,
) -> uuid.UUID:
    aid = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)
            VALUES (:id, :addr, :sym, :name, :dec, 'manual', :excluded)
            """
        ),
        {
            "id": str(aid),
            "addr": token_address.lower(),
            "sym": symbol,
            "name": name,
            "dec": decimals,
            "excluded": excluded,
        },
    )
    return aid


async def _insert_monitored_pair(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
) -> uuid.UUID:
    mid = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO monitored_pair (id, wallet_id, asset_id)"
            " VALUES (:id, :wid, :aid)"
        ),
        {"id": str(mid), "wid": str(wallet_id), "aid": str(asset_id)},
    )
    return mid


async def _insert_balance_observation(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int = 1_000_000_000_000_000_000,
    block_number: int = 12_345_678,
    observed_at: datetime | None = None,
) -> uuid.UUID:
    obs_id = uuid.uuid4()
    ts = observed_at or datetime.now(tz=UTC)
    await session.execute(
        text(
            """
            INSERT INTO balance_observation
              (id, wallet_id, asset_id, raw_amount, block_number, observed_at)
            VALUES (:id, :wid, :aid, :raw, :block, :ts)
            """
        ),
        {
            "id": str(obs_id),
            "wid": str(wallet_id),
            "aid": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
            "ts": ts,
        },
    )
    return obs_id


async def _insert_snapshot(
    session: AsyncSession,
    *,
    snapshotted_at: datetime | None = None,
    quality: str = "complete",
    published_at: datetime | None = None,
) -> uuid.UUID:
    sid = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        text(
            """
            INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at)
            VALUES (:id, :ts, :quality, :pub)
            """
        ),
        {
            "id": str(sid),
            "ts": snapshotted_at or now,
            "quality": quality,
            "pub": published_at or now,
        },
    )
    return sid


async def _insert_valuation_line(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int = 1_000_000_000_000_000_000,
    block_number: int = 12_345_678,
    price_usd: str | None = "1.0",
    value_usd: str | None = "1.0",
    observation_id: uuid.UUID | None = None,
) -> uuid.UUID:
    lid = uuid.uuid4()
    await session.execute(
        text(
            """
            INSERT INTO valuation_line
              (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number,
               price_usd, value_usd, observation_id)
            VALUES
              (:id, :sid, :wid, :aid, :raw, :block, :price, :value, :obs)
            """
        ),
        {
            "id": str(lid),
            "sid": str(snapshot_id),
            "wid": str(wallet_id),
            "aid": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
            "price": price_usd,
            "value": value_usd,
            "obs": str(observation_id) if observation_id else None,
        },
    )
    return lid


async def _insert_asset_metadata_revision(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    symbol: str = "TKN",
    name: str,
    decimals: int = 18,
    recorded_at: datetime | None = None,
) -> uuid.UUID:
    rid = uuid.uuid4()
    ts = recorded_at or datetime.now(tz=UTC)
    await session.execute(
        text(
            """
            INSERT INTO asset_metadata_revision
              (id, asset_id, symbol, name, decimals, source, recorded_at)
            VALUES (:id, :asset_id, :sym, :name, :dec, 'manual', :recorded_at)
            """
        ),
        {
            "id": str(rid),
            "asset_id": str(asset_id),
            "sym": symbol,
            "name": name,
            "dec": decimals,
            "recorded_at": ts,
        },
    )
    return rid


# ---------------------------------------------------------------------------
# JSON schema / record-type tests
# ---------------------------------------------------------------------------


async def test_export_json_schema_version(db_session: AsyncSession) -> None:
    """JSON portfolio export includes schema_version = 1 at the top level."""
    data = await export_current_portfolio(db_session)
    assert data.get("schema_version") == 1


async def test_export_csv_schema_version(db_session: AsyncSession) -> None:
    """CSV output begins with a metadata comment that declares schema_version: 1."""
    csv_text = await render_portfolio_csv(db_session)
    comment_lines = [l for l in csv_text.splitlines() if l.startswith("#")]
    assert comment_lines, "Expected at least one comment line starting with '#'"
    schema_version_line = next(
        (l for l in comment_lines if "schema_version" in l), None
    )
    assert schema_version_line is not None, (
        "No comment line containing 'schema_version' found"
    )
    assert "1" in schema_version_line


async def test_export_record_type_portfolio(db_session: AsyncSession) -> None:
    """JSON current-portfolio export contains record_type = 'current_portfolio'."""
    data = await export_current_portfolio(db_session)
    assert data.get("record_type") == "current_portfolio"


async def test_export_record_type_history(db_session: AsyncSession) -> None:
    """JSON full-history export contains record_type = 'full_history'."""
    data = await export_full_history(db_session)
    assert data.get("record_type") == "full_history"


# ---------------------------------------------------------------------------
# Exact value preservation
# ---------------------------------------------------------------------------


async def test_export_exact_value_preservation(db_session: AsyncSession) -> None:
    """A large Decimal raw_amount is exported as the exact string, not a float.

    The value 12345678901234567890 cannot be represented exactly as a float64;
    exporting it as a JSON number would silently round it.  The export must
    render it as the string "12345678901234567890".
    """
    raw = Decimal("12345678901234567890")
    wallet_id = await _insert_wallet(db_session, "0x" + "7" * 40)
    asset_id = await _insert_asset(
        db_session, token_address="0x" + "8" * 40, symbol="BIG"
    )
    await _insert_monitored_pair(db_session, wallet_id=wallet_id, asset_id=asset_id)
    await _insert_balance_observation(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        raw_amount=int(raw),
    )

    data = await export_current_portfolio(db_session)
    json_str = json.dumps(data)

    assert '"12345678901234567890"' in json_str, (
        "raw_amount must be serialised as the string '12345678901234567890'"
    )
    # Float64 representation of this value would contain scientific notation or
    # end in a rounding error — neither should appear.
    assert "1.2345678901234568e" not in json_str.lower()


# ---------------------------------------------------------------------------
# Unknown-vs-zero semantics
# ---------------------------------------------------------------------------


async def test_export_unknown_not_zero(db_session: AsyncSession) -> None:
    """A monitored wallet+asset with no balance observation exports balance as null,
    not as 0."""
    wallet_id = await _insert_wallet(db_session, "0x" + "e" * 40)
    asset_id = await _insert_asset(
        db_session, token_address="0x" + "f" * 40, symbol="UNK"
    )
    await _insert_monitored_pair(db_session, wallet_id=wallet_id, asset_id=asset_id)
    # Intentionally no _insert_balance_observation call.

    data = await export_current_portfolio(db_session)

    wallet_addr = "0x" + "e" * 40
    holding = next(
        (h for h in data.get("holdings", []) if h["wallet_address"] == wallet_addr),
        None,
    )
    assert holding is not None, (
        f"Expected an entry for wallet {wallet_addr!r} in the portfolio export"
    )
    assert holding["raw_amount"] is None, (
        "raw_amount should be null (None), not 0, when no observation exists"
    )


async def test_export_csv_unknown_not_zero(db_session: AsyncSession) -> None:
    """CSV raw_amount cell is empty string, not '0', when no observation exists."""
    wallet_addr = "0x" + "c" * 40
    wallet_id = await _insert_wallet(db_session, wallet_addr)
    asset_id = await _insert_asset(
        db_session, token_address="0x" + "d" * 40, symbol="UNK2"
    )
    await _insert_monitored_pair(db_session, wallet_id=wallet_id, asset_id=asset_id)
    # Intentionally no _insert_balance_observation call.

    csv_text = await render_portfolio_csv(db_session)

    # Strip comment lines before feeding to csv.DictReader.
    data_lines = [l for l in csv_text.splitlines() if not l.startswith("#")]
    reader = csv.DictReader(data_lines)
    rows = list(reader)

    match = next(
        (r for r in rows if r.get("wallet_address", "").lower() == wallet_addr), None
    )
    assert match is not None, (
        f"Expected a CSV row for wallet {wallet_addr!r}"
    )
    assert match.get("raw_amount") == "", (
        "raw_amount CSV cell should be empty string, not '0', for unknown balance"
    )


# ---------------------------------------------------------------------------
# Excluded assets
# ---------------------------------------------------------------------------


async def test_export_excluded_asset_omitted(db_session: AsyncSession) -> None:
    """Assets with excluded=True are absent from the current-portfolio export."""
    wallet_id = await _insert_wallet(db_session, "0x" + "a" * 40)

    included_id = await _insert_asset(
        db_session,
        token_address="0x" + "1" * 40,
        symbol="INCL",
        excluded=False,
    )
    excluded_id = await _insert_asset(
        db_session,
        token_address="0x" + "2" * 40,
        symbol="EXCL",
        excluded=True,
    )

    await _insert_monitored_pair(db_session, wallet_id=wallet_id, asset_id=included_id)
    await _insert_monitored_pair(db_session, wallet_id=wallet_id, asset_id=excluded_id)

    obs_included = await _insert_balance_observation(
        db_session, wallet_id=wallet_id, asset_id=included_id
    )
    await _insert_balance_observation(
        db_session, wallet_id=wallet_id, asset_id=excluded_id
    )

    data = await export_current_portfolio(db_session)
    symbols = [h["asset_symbol"] for h in data.get("holdings", [])]

    assert "INCL" in symbols, "Non-excluded asset must appear in the portfolio export"
    assert "EXCL" not in symbols, "Excluded asset must not appear in the portfolio export"


# ---------------------------------------------------------------------------
# Historical metadata revision
# ---------------------------------------------------------------------------


async def test_export_historical_metadata_revision(db_session: AsyncSession) -> None:
    """Each full-history record uses the asset name that was current at snapshot time,
    sourced from asset_metadata_revision."""
    base = datetime(2026, 5, 1, 0, 0, 0, tzinfo=UTC) 

    wallet_id = await _insert_wallet(db_session, "0x" + "5" * 40)
    asset_id = await _insert_asset(
        db_session,
        token_address="0x" + "6" * 40,
        symbol="RNM",
        name="TokenV1",
    )

    # Revision 1 recorded at base — asset is named "TokenV1".
    await _insert_asset_metadata_revision(
        db_session,
        asset_id=asset_id,
        symbol="RNM",
        name="TokenV1",
        recorded_at=base,
    )

    # Snapshot 1 at base + 1 h — should resolve name to "TokenV1".
    snap1_at = base + timedelta(hours=1)
    snap1_id = await _insert_snapshot(db_session, snapshotted_at=snap1_at)
    obs1_id = await _insert_balance_observation(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observed_at=snap1_at,
    )
    await _insert_valuation_line(
        db_session,
        snapshot_id=snap1_id,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs1_id,
    )

    # Asset renamed at base + 2 h.
    await _insert_asset_metadata_revision(
        db_session,
        asset_id=asset_id,
        symbol="RNM",
        name="TokenV2",
        recorded_at=base + timedelta(hours=2),
    )

    # Snapshot 2 at base + 3 h — should resolve name to "TokenV2".
    snap2_at = base + timedelta(hours=3)
    snap2_id = await _insert_snapshot(db_session, snapshotted_at=snap2_at)
    obs2_id = await _insert_balance_observation(
        db_session,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observed_at=snap2_at,
    )
    await _insert_valuation_line(
        db_session,
        snapshot_id=snap2_id,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs2_id,
    )

    data = await export_full_history(db_session)

    snap1_entry = next(
        (s for s in data.get("snapshots", []) if s["snapshot_id"] == str(snap1_id)),
        None,
    )
    snap2_entry = next(
        (s for s in data.get("snapshots", []) if s["snapshot_id"] == str(snap2_id)),
        None,
    )

    assert snap1_entry is not None, "Snapshot 1 must appear in full-history export"
    assert snap2_entry is not None, "Snapshot 2 must appear in full-history export"

    snap1_names = [line["asset_name"] for line in snap1_entry.get("lines", [])]
    snap2_names = [line["asset_name"] for line in snap2_entry.get("lines", [])]

    assert "TokenV1" in snap1_names, (
        "Snapshot 1 lines should use the asset name active at snapshot time ('TokenV1')"
    )
    assert "TokenV2" in snap2_names, (
        "Snapshot 2 lines should use the asset name active at snapshot time ('TokenV2')"
    )


# ---------------------------------------------------------------------------
# Full-history completeness and ordering
# ---------------------------------------------------------------------------


async def test_export_full_history_all_snapshots(db_session: AsyncSession) -> None:
    """Full-history export includes all published snapshots, ordered by
    snapshotted_at ascending."""
    base = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC) 

    # Insert 3 published snapshots in a non-chronological insertion order.
    ts_mid = base
    ts_early = base - timedelta(hours=4)
    ts_late = base + timedelta(hours=4)

    inserted_ids = []
    for ts in (ts_mid, ts_late, ts_early):
        sid = await _insert_snapshot(db_session, snapshotted_at=ts)
        inserted_ids.append(str(sid))

    data = await export_full_history(db_session)
    snapshots = data.get("snapshots", [])

    returned_ids = [s["snapshot_id"] for s in snapshots]
    for expected_id in inserted_ids:
        assert expected_id in returned_ids, (
            f"Published snapshot {expected_id} missing from full-history export"
        )

    # Timestamps must be in ascending order across the full result set.
    times = [s["snapshotted_at"] for s in snapshots]
    assert times == sorted(times), (
        "Full-history export snapshots must be ordered by snapshotted_at ascending"
    )
