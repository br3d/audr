"""Materialize history-point summaries on snapshot publication (T069 / US3 / AUD-82).

materialize_history_point() is called immediately after publish_valuation_snapshot().
It reads the newly published snapshot's valuation_lines, computes aggregate stats,
and inserts a history_point row.

Rules:
- Excluded assets do not appear in valuation_lines and are not counted.
- total_value_usd is the sum of non-null value_usd lines (NULL lines are unknown).
- has_gap is true when quality is 'stale', 'partial', or 'gaps' (some values are unknown).
- is_canonical is always true on first write; the canonicality job clears it later.
- Idempotent: a second call for the same snapshot_id returns the existing row ID.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class HistoryPointResult:
    history_point_id: uuid.UUID
    snapshot_id: uuid.UUID
    total_value_usd: Decimal | None
    quality: str
    included_wallet_count: int
    included_asset_count: int
    has_gap: bool
    is_canonical: bool
    created: bool  # False when the row already existed (idempotent replay)


async def materialize_history_point(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
) -> HistoryPointResult:
    """Build and persist a history_point for the given published snapshot.

    Idempotent: if a history_point already exists for this snapshot_id, the
    existing row is returned with created=False.
    """
    # Idempotency check.
    existing = await session.execute(
        sa.text(
            """
            SELECT id, total_value_usd::text, quality,
                   included_wallet_count, included_asset_count,
                   has_gap, is_canonical
            FROM history_point WHERE snapshot_id = :sid
            """
        ),
        {"sid": str(snapshot_id)},
    )
    row = existing.first()
    if row is not None:
        total = Decimal(row[1]) if row[1] is not None else None
        return HistoryPointResult(
            history_point_id=uuid.UUID(str(row[0])),
            snapshot_id=snapshot_id,
            total_value_usd=total,
            quality=row[2],
            included_wallet_count=row[3],
            included_asset_count=row[4],
            has_gap=row[5],
            is_canonical=row[6],
            created=False,
        )

    lines = await _read_snapshot_lines(session, snapshot_id)
    if not lines:
        raise ValueError(f"snapshot {snapshot_id} has no valuation lines — cannot materialize")

    snapshotted_at = await _read_snapshotted_at(session, snapshot_id)
    quality = await _read_quality(session, snapshot_id)

    total_value_usd: Decimal | None = None
    wallet_ids: set[str] = set()
    asset_ids: set[str] = set()

    for line in lines:
        wallet_ids.add(line["wallet_id"])
        asset_ids.add(line["asset_id"])
        if line["value_usd"] is not None:
            v = Decimal(str(line["value_usd"]))
            total_value_usd = (total_value_usd or Decimal(0)) + v

    has_gap = quality in ("stale", "partial", "gaps", "unknown")
    point_id = uuid.uuid4()

    await session.execute(
        sa.text(
            """
            INSERT INTO history_point
              (id, snapshot_id, snapshotted_at, total_value_usd,
               quality, included_wallet_count, included_asset_count,
               has_gap, is_canonical)
            VALUES
              (:id, :sid, :ts, :total, :quality, :wc, :ac, :gap, true)
            """
        ),
        {
            "id": str(point_id),
            "sid": str(snapshot_id),
            "ts": snapshotted_at,
            "total": str(total_value_usd) if total_value_usd is not None else None,
            "quality": quality,
            "wc": len(wallet_ids),
            "ac": len(asset_ids),
            "gap": has_gap,
        },
    )

    logger.info(
        "history_point materialized id=%s snapshot=%s quality=%s total_usd=%s"
        " wallets=%d assets=%d has_gap=%s",
        point_id,
        snapshot_id,
        quality,
        total_value_usd,
        len(wallet_ids),
        len(asset_ids),
        has_gap,
    )

    return HistoryPointResult(
        history_point_id=point_id,
        snapshot_id=snapshot_id,
        total_value_usd=total_value_usd,
        quality=quality,
        included_wallet_count=len(wallet_ids),
        included_asset_count=len(asset_ids),
        has_gap=has_gap,
        is_canonical=True,
        created=True,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _read_snapshot_lines(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
) -> list[dict[str, object]]:
    result = await session.execute(
        sa.text(
            """
            SELECT
                vl.wallet_id::text,
                vl.asset_id::text,
                vl.value_usd::text,
                vl.observation_id::text
            FROM valuation_line vl
            WHERE vl.snapshot_id = :sid
            """
        ),
        {"sid": str(snapshot_id)},
    )
    return [
        {
            "wallet_id": row[0],
            "asset_id": row[1],
            "value_usd": row[2],
            "observation_id": row[3],
        }
        for row in result
    ]


async def _read_snapshotted_at(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
) -> object:
    result = await session.execute(
        sa.text("SELECT snapshotted_at FROM valuation_snapshot WHERE id = :sid"),
        {"sid": str(snapshot_id)},
    )
    row = result.first()
    if row is None:
        raise ValueError(f"snapshot {snapshot_id} not found")
    return row[0]


async def _read_quality(
    session: AsyncSession,
    snapshot_id: uuid.UUID,
) -> str:
    result = await session.execute(
        sa.text("SELECT quality FROM valuation_snapshot WHERE id = :sid"),
        {"sid": str(snapshot_id)},
    )
    row = result.first()
    if row is None:
        raise ValueError(f"snapshot {snapshot_id} not found")
    return str(row[0])
