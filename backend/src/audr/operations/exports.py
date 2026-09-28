"""Streamed current-portfolio and full-history JSON/CSV exports (T084 / US4)."""

from __future__ import annotations

import csv
import io
from datetime import datetime
from datetime import timezone as _tz
_UTC = _tz.utc
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession


async def export_current_portfolio(session: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Export the current portfolio state as a structured dict.

    Uses the latest balance_observation per (wallet, asset) pair.
    Assets with ``excluded = true`` are omitted entirely.
    Unknown balance (no observation) is represented as ``None``, never ``0``.

    Returns::

        {
            "schema_version": 1,
            "record_type": "current_portfolio",
            "exported_at": "<ISO-8601>",
            "holdings": [
                {
                    "wallet_address": str,
                    "asset_symbol": str,
                    "asset_name": str,
                    "token_address": str,
                    "raw_amount": str | None,
                    "decimals": int,
                },
                ...
            ]
        }
    """
    result = await session.execute(
        sa.text(
            """
            SELECT
                w.address         AS wallet_address,
                a.symbol          AS asset_symbol,
                a.name            AS asset_name,
                a.token_address,
                bo.raw_amount,
                COALESCE(a.decimals_override, a.decimals) AS decimals
            FROM wallet w
            CROSS JOIN asset a
            LEFT JOIN LATERAL (
                SELECT raw_amount
                FROM balance_observation
                WHERE wallet_id = w.id
                  AND asset_id  = a.id
                ORDER BY observed_at DESC
                LIMIT 1
            ) bo ON true
            WHERE a.excluded = false
            ORDER BY w.address, a.symbol
            """
        )
    )
    rows = result.fetchall()

    holdings = []
    for row in rows:
        raw_amount = row[4]  # Decimal or None
        holdings.append(
            {
                "wallet_address": str(row[0]),
                "asset_symbol": str(row[1]),
                "asset_name": str(row[2]),
                "token_address": str(row[3]),
                "raw_amount": str(Decimal(raw_amount)) if raw_amount is not None else None,
                "decimals": int(row[5]),
            }
        )

    return {
        "schema_version": 1,
        "record_type": "current_portfolio",
        "exported_at": datetime.now(_UTC).isoformat(),
        "holdings": holdings,
    }


async def export_full_history(session: AsyncSession) -> dict:  # type: ignore[type-arg]
    """Export the complete valuation snapshot history as a structured dict.

    Snapshots are ordered by ``snapshotted_at`` ASC.  Each line's asset name
    comes from the ``asset_metadata_revision`` recorded latest before the
    snapshot's ``snapshotted_at``; falls back to the current ``asset.name`` when
    no revision predates the snapshot.

    Returns::

        {
            "schema_version": 1,
            "record_type": "full_history",
            "exported_at": "<ISO-8601>",
            "snapshots": [
                {
                    "snapshot_id": str,
                    "snapshotted_at": str,
                    "quality": str,
                    "lines": [
                        {
                            "wallet_address": str,
                            "asset_symbol": str,
                            "asset_name": str,
                            "raw_amount": str | None,
                            "price_usd": str | None,
                            "decimals": int,
                        },
                        ...
                    ]
                },
                ...
            ]
        }
    """
    snapshots_result = await session.execute(
        sa.text(
            """
            SELECT id, snapshotted_at, quality
            FROM valuation_snapshot
            ORDER BY snapshotted_at ASC
            """
        )
    )
    snapshot_rows = snapshots_result.fetchall()

    snapshots = []
    for snap_row in snapshot_rows:
        snap_id = snap_row[0]
        snapshotted_at: datetime = snap_row[1]
        quality: str = snap_row[2]

        lines_result = await session.execute(
            sa.text(
                """
                SELECT
                    w.address                          AS wallet_address,
                    a.symbol                           AS asset_symbol,
                    COALESCE(amr.name, a.name)         AS asset_name,
                    vl.raw_amount,
                    vl.price_usd,
                    COALESCE(a.decimals_override, a.decimals) AS decimals
                FROM valuation_line vl
                JOIN wallet w ON w.id = vl.wallet_id
                JOIN asset a  ON a.id = vl.asset_id
                LEFT JOIN LATERAL (
                    SELECT name
                    FROM asset_metadata_revision
                    WHERE asset_id  = a.id
                      AND recorded_at <= :snapshotted_at
                    ORDER BY recorded_at DESC
                    LIMIT 1
                ) amr ON true
                WHERE vl.snapshot_id = :snapshot_id
                ORDER BY w.address, a.symbol
                """
            ),
            {"snapshot_id": snap_id, "snapshotted_at": snapshotted_at},
        )

        lines = []
        for line_row in lines_result.fetchall():
            raw_amount = line_row[3]
            price_usd  = line_row[4]
            lines.append(
                {
                    "wallet_address": str(line_row[0]),
                    "asset_symbol": str(line_row[1]),
                    "asset_name": str(line_row[2]),
                    "raw_amount": str(Decimal(raw_amount)) if raw_amount is not None else None,
                    "price_usd": str(Decimal(price_usd)) if price_usd is not None else None,
                    "decimals": int(line_row[5]),
                }
            )

        snapshots.append(
            {
                "snapshot_id": str(snap_id),
                "snapshotted_at": snapshotted_at.isoformat(),
                "quality": quality,
                "lines": lines,
            }
        )

    return {
        "schema_version": 1,
        "record_type": "full_history",
        "exported_at": datetime.now(_UTC).isoformat(),
        "snapshots": snapshots,
    }


async def render_portfolio_csv(session: AsyncSession) -> str:
    """Render the current portfolio as a CSV string.

    Format::

        # schema_version: 1
        wallet_address,asset_symbol,asset_name,token_address,raw_amount,decimals
        0xABC...,ETH,Ether,0x000...,1000000000000000000,18
        0xABC...,USDC,USD Coin,0x123...,,6

    Unknown balances are serialised as empty strings, not ``"0"``.
    Uses Python's ``csv`` module; no float arithmetic.
    """
    portfolio = await export_current_portfolio(session)

    buf = io.StringIO()
    buf.write("# schema_version: 1\n")

    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        ["wallet_address", "asset_symbol", "asset_name", "token_address", "raw_amount", "decimals"]
    )

    for holding in portfolio["holdings"]:
        raw_amount_cell = holding["raw_amount"] if holding["raw_amount"] is not None else ""
        writer.writerow(
            [
                holding["wallet_address"],
                holding["asset_symbol"],
                holding["asset_name"],
                holding["token_address"],
                raw_amount_cell,
                holding["decimals"],
            ]
        )

    return buf.getvalue()
