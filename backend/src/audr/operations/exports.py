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

from audr.operations.csv_safe import sanitize as _sanitize


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


async def export_full_history(
    session: AsyncSession,
    *,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> dict:  # type: ignore[type-arg]
    """Export the complete valuation snapshot history as a structured dict.

    Uses a single JOIN query (not N+1).  Optional *from_dt* / *to_dt* bound the
    ``snapshotted_at`` range (both inclusive).

    Returns::

        {
            "schema_version": 1,
            "record_type": "full_history",
            "exported_at": "<ISO-8601>",
            "snapshots": [...]
        }
    """
    where_clauses = []
    params: dict[str, object] = {}
    if from_dt is not None:
        where_clauses.append("vs.snapshotted_at >= :from_dt")
        params["from_dt"] = from_dt
    if to_dt is not None:
        where_clauses.append("vs.snapshotted_at <= :to_dt")
        params["to_dt"] = to_dt
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    result = await session.execute(
        sa.text(
            f"""
            SELECT
                vs.id                              AS snapshot_id,
                vs.snapshotted_at,
                vs.quality,
                w.address                          AS wallet_address,
                a.symbol                           AS asset_symbol,
                COALESCE(amr.name, a.name)         AS asset_name,
                vl.raw_amount,
                vl.price_usd,
                COALESCE(a.decimals_override, a.decimals) AS decimals
            FROM valuation_snapshot vs
            LEFT JOIN valuation_line vl ON vl.snapshot_id = vs.id
            LEFT JOIN wallet w ON w.id = vl.wallet_id
            LEFT JOIN asset a ON a.id = vl.asset_id
            LEFT JOIN LATERAL (
                SELECT name
                FROM asset_metadata_revision
                WHERE asset_id = a.id
                  AND recorded_at <= vs.snapshotted_at
                ORDER BY recorded_at DESC
                LIMIT 1
            ) amr ON true
            {where_sql}
            ORDER BY vs.snapshotted_at ASC, w.address, a.symbol
            """  # noqa: S608
        ),
        params,
    )

    snapshots: list[dict] = []  # type: ignore[type-arg]
    current_snap_id: str | None = None
    current_snap: dict | None = None  # type: ignore[type-arg]

    for row in result:
        snap_id_str = str(row[0])
        if snap_id_str != current_snap_id:
            if current_snap is not None:
                snapshots.append(current_snap)
            current_snap_id = snap_id_str
            current_snap = {
                "snapshot_id": snap_id_str,
                "snapshotted_at": row[1].isoformat(),
                "quality": row[2],
                "lines": [],
            }
        # row[3] (wallet_address) is None when the snapshot has no lines
        if row[3] is not None and current_snap is not None:
            raw_amount = row[6]
            price_usd = row[7]
            current_snap["lines"].append(
                {
                    "wallet_address": str(row[3]),
                    "asset_symbol": str(row[4]),
                    "asset_name": str(row[5]),
                    "raw_amount": str(Decimal(raw_amount)) if raw_amount is not None else None,
                    "price_usd": str(Decimal(price_usd)) if price_usd is not None else None,
                    "decimals": int(row[8]),
                }
            )

    if current_snap is not None:
        snapshots.append(current_snap)

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
                _sanitize(holding["asset_symbol"]),
                _sanitize(holding["asset_name"]),
                holding["token_address"],
                raw_amount_cell,
                holding["decimals"],
            ]
        )

    return buf.getvalue()


async def render_history_csv(
    session: AsyncSession,
    *,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
) -> str:
    """Render the full valuation history as a CSV string.

    Format::

        # schema_version: 1
        snapshot_id,snapshotted_at,quality,wallet_address,asset_symbol,asset_name,raw_amount,price_usd,decimals
    """
    history = await export_full_history(session, from_dt=from_dt, to_dt=to_dt)

    buf = io.StringIO()
    buf.write("# schema_version: 1\n")

    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(
        ["snapshot_id", "snapshotted_at", "quality", "wallet_address", "asset_symbol", "asset_name", "raw_amount", "price_usd", "decimals"]
    )

    for snapshot in history["snapshots"]:
        for line in snapshot["lines"]:
            writer.writerow(
                [
                    snapshot["snapshot_id"],
                    snapshot["snapshotted_at"],
                    snapshot["quality"],
                    line["wallet_address"],
                    _sanitize(line["asset_symbol"]),
                    _sanitize(line["asset_name"]),
                    line["raw_amount"] if line["raw_amount"] is not None else "",
                    line["price_usd"] if line["price_usd"] is not None else "",
                    line["decimals"],
                ]
            )

    return buf.getvalue()
