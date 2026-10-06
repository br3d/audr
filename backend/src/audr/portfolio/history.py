"""Materialize history-point summaries on snapshot publication (T069 / US3 / AUD-82).

materialize_history_point() is called immediately after publish_valuation_snapshot().
It reads the newly published snapshot's valuation_lines, computes aggregate stats,
and inserts a history_point row.

Rules:
- total_value_usd is the sum of non-null value_usd lines (NULL lines are unknown).
- has_gap is true when quality is 'stale', 'partial', or 'gaps' (some values are unknown).
- is_canonical is always true on first write; the canonicality job clears it later.
- Idempotent: a second call for the same snapshot_id returns the existing row ID.

rematerialize_history_points():
  history_point.total_value_usd and the two counts are a denormalised aggregate of
  the snapshot's valuation_lines. Anything that deletes lines from an already
  published snapshot — deleting a wallet, purging a provider — must call this so
  the aggregate still describes the lines that remain (AUD-454). Points left with
  no lines at all are dropped: an empty snapshot has no total to report.
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


async def rematerialize_history_points(
    session: AsyncSession,
    snapshot_ids: list[uuid.UUID] | list[str],
) -> dict[str, int]:
    """Re-derive history_point aggregates for *snapshot_ids* from their lines.

    history_point stores the total and the wallet/asset counts that
    materialize_history_point() computed from the snapshot's valuation_lines at
    publication. Deleting lines afterwards leaves that aggregate describing rows
    that are no longer there: a chart point keeps the value of a wallet the owner
    deleted, and GET /history's exclusion re-cut — which subtracts a sum taken
    from the surviving lines — then subtracts from a total those lines never
    added up to (AUD-454).

    Recomputes total_value_usd (NULL when no surviving line carries a value, i.e.
    the point's value is unknown rather than zero) and the two counts, and deletes
    points whose snapshot has no lines left.

    Returns {"updated": n, "deleted": n}. A no-op for an empty id list.
    """
    if not snapshot_ids:
        return {"updated": 0, "deleted": 0}

    ids = [str(sid) for sid in snapshot_ids]

    deleted = await session.execute(
        sa.text(
            """
            DELETE FROM history_point hp
            WHERE hp.snapshot_id IN :snapshot_ids
              AND NOT EXISTS (
                SELECT 1 FROM valuation_line vl WHERE vl.snapshot_id = hp.snapshot_id
              )
            """
        ).bindparams(sa.bindparam("snapshot_ids", expanding=True)),
        {"snapshot_ids": ids},
    )

    updated = await session.execute(
        sa.text(
            """
            UPDATE history_point hp
            SET total_value_usd = agg.total_value_usd,
                included_wallet_count = agg.wallet_count,
                included_asset_count = agg.asset_count
            FROM (
                SELECT vl.snapshot_id,
                       SUM(vl.value_usd)             AS total_value_usd,
                       COUNT(DISTINCT vl.wallet_id)  AS wallet_count,
                       COUNT(DISTINCT vl.asset_id)   AS asset_count
                FROM valuation_line vl
                WHERE vl.snapshot_id IN :snapshot_ids
                GROUP BY vl.snapshot_id
            ) agg
            WHERE hp.snapshot_id = agg.snapshot_id
              AND (
                hp.total_value_usd IS DISTINCT FROM agg.total_value_usd
                OR hp.included_wallet_count IS DISTINCT FROM agg.wallet_count
                OR hp.included_asset_count IS DISTINCT FROM agg.asset_count
              )
            """
        ).bindparams(sa.bindparam("snapshot_ids", expanding=True)),
        {"snapshot_ids": ids},
    )

    counts = {"updated": int(updated.rowcount or 0), "deleted": int(deleted.rowcount or 0)}
    if counts["updated"] or counts["deleted"]:
        logger.info(
            "history_point re-materialized after line deletion: updated=%d deleted=%d",
            counts["updated"],
            counts["deleted"],
        )
    return counts


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
