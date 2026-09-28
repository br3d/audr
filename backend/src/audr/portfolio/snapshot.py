"""Valuation snapshot composition and publishing (T056/T057 / US2 / AUD-69/AUD-70).

publish_valuation_snapshot() atomically:
  1. Reads current holdings (latest balance per wallet/asset).
  2. Reads the most recent complete quote_set's observations.
  3. Computes quality: complete / partial / stale / unknown.
  4. Inserts a valuation_snapshot row.
  5. Inserts one valuation_line per holding, with price/value where available.
  6. Marks the snapshot published.

Rules:
- Unknown ≠ zero: holdings without prices get NULL price_usd/value_usd lines.
- Excluded assets are excluded from the snapshot (neither line nor quality count).
- All arithmetic uses Python Decimal — never float.
- The snapshot is immutable once published_at is set.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.portfolio.money import format_decimal, quantity_to_usd, raw_to_quantity

logger = logging.getLogger(__name__)


@dataclass
class HoldingRow:
    wallet_id: uuid.UUID
    asset_id: uuid.UUID
    token_address: str
    raw_amount: int
    block_number: int
    decimals: int


@dataclass
class SnapshotResult:
    snapshot_id: uuid.UUID
    quality: str
    line_count: int
    priced_count: int


async def publish_valuation_snapshot(session: AsyncSession) -> SnapshotResult:
    """Create and persist a new published valuation snapshot.

    Returns the snapshot metadata.  Raises if there are no holdings at all.
    """
    holdings = await _get_current_holdings(session)
    if not holdings:
        raise ValueError("no holdings available — cannot publish empty snapshot")

    latest_prices = await _get_latest_prices(session)

    quality, priced_count = _compute_quality(holdings, latest_prices)

    snapshot_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            """
            INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at, created_at)
            VALUES (:id, :now, :quality, :now, :now)
            """
        ),
        {"id": str(snapshot_id), "now": now, "quality": quality},
    )

    for holding in holdings:
        price_decimal = latest_prices.get(holding.asset_id)
        quantity = raw_to_quantity(holding.raw_amount, holding.decimals)

        if price_decimal is not None:
            value_decimal: Decimal | None = quantity_to_usd(quantity, price_decimal)
        else:
            value_decimal = None

        await session.execute(
            sa.text(
                """
                INSERT INTO valuation_line
                  (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number,
                   price_usd, value_usd, created_at)
                VALUES
                  (:id, :snap, :wallet, :asset, :raw, :block, :price, :value, :now)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "snap": str(snapshot_id),
                "wallet": str(holding.wallet_id),
                "asset": str(holding.asset_id),
                "raw": str(holding.raw_amount),
                "block": holding.block_number,
                "price": str(price_decimal) if price_decimal is not None else None,
                "value": format_decimal(value_decimal) if value_decimal is not None else None,
                "now": now,
            },
        )

    await session.flush()

    logger.info(
        "valuation snapshot published id=%s quality=%s lines=%d priced=%d",
        snapshot_id,
        quality,
        len(holdings),
        priced_count,
    )

    return SnapshotResult(
        snapshot_id=snapshot_id,
        quality=quality,
        line_count=len(holdings),
        priced_count=priced_count,
    )


async def get_latest_snapshot_lines(
    session: AsyncSession,
) -> list[dict[str, object]]:
    """Return valuation lines from the most recently published snapshot.

    Returns an empty list when no published snapshot exists.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT
                vl.wallet_id,
                vl.asset_id,
                a.token_address,
                a.symbol,
                a.name,
                COALESCE(a.decimals_override, a.decimals) AS effective_decimals,
                vl.raw_amount::text,
                vl.block_number,
                vl.price_usd::text,
                vl.value_usd::text
            FROM valuation_line vl
            JOIN valuation_snapshot vs ON vs.id = vl.snapshot_id
            JOIN asset a ON a.id = vl.asset_id
            WHERE vs.published_at = (
                SELECT MAX(published_at) FROM valuation_snapshot
                WHERE published_at IS NOT NULL
            )
            ORDER BY vl.value_usd DESC NULLS LAST
            """
        )
    )
    rows = []
    for row in result:
        rows.append(
            {
                "wallet_id": row[0],
                "asset_id": row[1],
                "token_address": row[2],
                "symbol": row[3],
                "name": row[4],
                "effective_decimals": row[5],
                "raw_amount": row[6],
                "block_number": row[7],
                "price_usd": row[8],
                "value_usd": row[9],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_current_holdings(session: AsyncSession) -> list[HoldingRow]:
    """Latest non-zero balance observation per (wallet, asset), skipping excluded assets."""
    result = await session.execute(
        sa.text(
            """
            SELECT
                w.id   AS wallet_id,
                a.id   AS asset_id,
                a.token_address,
                bo.raw_amount::bigint,
                bo.block_number,
                COALESCE(a.decimals_override, a.decimals) AS effective_decimals
            FROM balance_observation bo
            JOIN wallet w ON w.id = bo.wallet_id
            JOIN asset  a ON a.id = bo.asset_id
            WHERE NOT COALESCE(a.excluded, false)
              AND bo.observed_at = (
                  SELECT MAX(bo2.observed_at)
                  FROM balance_observation bo2
                  WHERE bo2.wallet_id = bo.wallet_id
                    AND bo2.asset_id  = bo.asset_id
              )
              AND bo.raw_amount > 0
            """
        )
    )
    return [
        HoldingRow(
            wallet_id=uuid.UUID(str(row[0])),
            asset_id=uuid.UUID(str(row[1])),
            token_address=row[2],
            raw_amount=int(row[3]),
            block_number=int(row[4]),
            decimals=int(row[5]),
        )
        for row in result
    ]


async def _get_latest_prices(session: AsyncSession) -> dict[uuid.UUID, Decimal]:
    """Return {asset_id: price_usd} from the most recent complete quote_set."""
    result = await session.execute(
        sa.text(
            """
            SELECT qo.asset_id, qo.price_usd::text
            FROM quote_observation qo
            JOIN quote_set qs ON qs.id = qo.quote_set_id
            WHERE qs.status = 'complete'
              AND qs.fetched_at = (
                  SELECT MAX(qs2.fetched_at)
                  FROM quote_set qs2
                  JOIN quote_observation qo2 ON qo2.quote_set_id = qs2.id
                  WHERE qs2.status = 'complete'
              )
            """
        )
    )
    return {
        uuid.UUID(str(row[0])): Decimal(str(row[1]))
        for row in result
    }


def _compute_quality(
    holdings: list[HoldingRow],
    prices: dict[uuid.UUID, Decimal],
) -> tuple[str, int]:
    """Return (quality_string, priced_count).

    Quality:
      unknown  — no holdings
      stale    — holdings exist but no prices available at all
      partial  — some holdings have prices, some do not
      complete — every holding has a price
    """
    if not holdings:
        return "unknown", 0

    priced = sum(1 for h in holdings if h.asset_id in prices)

    if priced == 0:
        return "stale", 0
    if priced < len(holdings):
        return "partial", priced
    return "complete", priced
