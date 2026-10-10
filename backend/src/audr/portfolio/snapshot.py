"""Valuation snapshot composition and publishing (T056/T057 / US2 / AUD-69/AUD-70).

publish_valuation_snapshot() atomically:
  1. Reads current holdings (latest balance per wallet/asset).
  2. Reads the most recent complete quote_set's observations.
  3. Computes quality: complete / partial / stale / unknown.
  4. Computes a deterministic input_key from the exact inputs (holding
     observation ids + quote_set ids) and returns the existing snapshot for
     that key if one was already published, instead of inserting a duplicate.
  5. Inserts a valuation_snapshot row.
  6. Inserts one valuation_line per holding, with price/value where available.
  7. Marks the snapshot published.

Rules:
- Unknown ≠ zero: holdings without prices get NULL price_usd/value_usd lines.
- Excluded assets still get a line; exclusion is a read-time concern (AUD-448).
  The snapshot records what the wallets actually held, so GET /portfolio and
  GET /history can honour the *current* exclusion set over any past snapshot
  and the owner can un-exclude an asset without waiting for a re-scan.
  Exclusion still shapes `quality` and the priced counts, which describe the
  part of the portfolio that counts towards the total.
- All arithmetic uses Python Decimal — never float.
- The snapshot is immutable once published_at is set.
- Publication is idempotent by exact input key: the same observation set and
  quote data always produce the same snapshot, never a new one.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.init_key import get_master_key
from audr.portfolio.money import format_decimal, quantity_to_usd, raw_to_quantity
from audr.wallets.service import decrypt_address

logger = logging.getLogger(__name__)


@dataclass
class HoldingRow:
    wallet_id: uuid.UUID
    asset_id: uuid.UUID
    observation_id: uuid.UUID
    token_address: str
    raw_amount: int
    block_number: int
    decimals: int
    block_time: datetime | None = None
    # True when the asset's most recent quote_refresh attempt asked the
    # provider about it and the provider didn't know it (AUD-361) — as
    # opposed to the asset simply never having been asked about.
    price_unavailable: bool = False
    # The asset's current `excluded` flag. The line is still written — only
    # quality, the priced counts and every read-side total skip it (AUD-448).
    excluded: bool = False


@dataclass
class SnapshotResult:
    snapshot_id: uuid.UUID
    quality: str
    line_count: int
    priced_count: int
    created: bool = True  # False when an identical input key was already published


async def publish_valuation_snapshot(session: AsyncSession) -> SnapshotResult:
    """Create and persist a new published valuation snapshot.

    Idempotent by exact input key: if the current holdings (by observation
    id) and quote data (by quote_set id) match an already-published
    snapshot, that snapshot is returned with created=False instead of
    inserting a duplicate.

    Returns the snapshot metadata.  Raises if there are no holdings at all.
    """
    holdings = await _get_current_holdings(session)
    if not holdings:
        raise ValueError("no holdings available — cannot publish empty snapshot")

    latest_prices, quote_set_ids = await _get_latest_prices(session)

    # Quality describes the part of the portfolio that counts towards the
    # total, so it is computed over the included holdings only — an excluded
    # unpriceable dust token must not downgrade the snapshot or null out
    # total_usd (AUD-448). Every holding still gets a line below.
    included = [h for h in holdings if not h.excluded]
    quality, priced_count = _compute_quality(included, latest_prices)

    input_key = _compute_input_key(holdings, quote_set_ids)

    existing = await _get_snapshot_by_input_key(session, input_key)
    if existing is not None:
        return existing

    snapshot_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            """
            INSERT INTO valuation_snapshot
              (id, snapshotted_at, quality, published_at, created_at, input_key)
            VALUES (:id, :now, :quality, :now, :now, :input_key)
            """
        ),
        {"id": str(snapshot_id), "now": now, "quality": quality, "input_key": input_key},
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
                   block_time, price_usd, value_usd, observation_id, created_at)
                VALUES
                  (:id, :snap, :wallet, :asset, :raw, :block, :block_time,
                   :price, :value, :obs, :now)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "snap": str(snapshot_id),
                "wallet": str(holding.wallet_id),
                "asset": str(holding.asset_id),
                "raw": str(holding.raw_amount),
                "block": holding.block_number,
                "block_time": holding.block_time,
                "price": str(price_decimal) if price_decimal is not None else None,
                "value": format_decimal(value_decimal) if value_decimal is not None else None,
                "obs": str(holding.observation_id),
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
        created=True,
    )


async def get_latest_snapshot_lines(
    session: AsyncSession,
) -> list[dict[str, object]]:
    """Return valuation lines from the most recently published snapshot.

    Excluded assets are filtered out here. Snapshots record them now (AUD-448)
    so the dashboard can toggle exclusion without waiting for a re-scan, but
    this feeds GET /portfolio/holdings, whose rows carry no `included` flag —
    returning them would silently put excluded assets back into a list that
    has no way to say they don't count.

    Returns an empty list when no published snapshot exists.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT
                vl.wallet_id,
                w.address_ciphertext,
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
            JOIN wallet w ON w.id = vl.wallet_id
            WHERE vs.published_at = (
                SELECT MAX(published_at) FROM valuation_snapshot
                WHERE published_at IS NOT NULL
            )
              AND NOT COALESCE(a.excluded, false)
            ORDER BY vl.value_usd DESC NULLS LAST
            """
        )
    )
    fetched = result.fetchall()
    rows = []
    if fetched:
        key = await get_master_key(session)
    for row in fetched:
        wallet_id = uuid.UUID(str(row[0]))
        rows.append(
            {
                "wallet_id": row[0],
                "wallet_address": decrypt_address(row[1], wallet_id, key),
                "asset_id": row[2],
                "token_address": row[3],
                "symbol": row[4],
                "name": row[5],
                "effective_decimals": row[6],
                "raw_amount": row[7],
                "block_number": row[8],
                "price_usd": row[9],
                "value_usd": row[10],
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_current_holdings(session: AsyncSession) -> list[HoldingRow]:
    """Latest non-zero balance observation per (wallet, asset).

    Excluded assets are returned too, carrying their current ``excluded``
    flag (AUD-448). They used to be filtered out here, which made exclusion
    destructive: once a snapshot had been published without the line, the
    asset's value was gone from that point in history, so un-excluding it
    could not restore anything until the next balance scan, and a read-time
    filter had nothing to filter. Recording the line and deciding at read
    time keeps the toggle reversible and instant in both directions.

    Only ``active`` wallets contribute (AUD-446). A stopped wallet is never
    scanned again by handle_balance_scan, so its last observation is frozen at
    the block it was stopped on. Carrying those lines into new snapshots pinned
    every one of its assets below the live wallets' block_number, which
    api/portfolio.py reports as a permanent "Stale" badge that no amount of
    "Refresh balances" can ever clear. Excluding them also restores the
    behaviour the Wallets page already promises: "Stopping tracking removes
    this address from balance calculations but retains its historical records"
    — the observation rows stay, they just stop being current holdings.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT
                w.id   AS wallet_id,
                a.id   AS asset_id,
                a.token_address,
                bo.raw_amount::numeric,
                bo.block_number,
                bo.block_time,
                COALESCE(a.decimals_override, a.decimals) AS effective_decimals,
                bo.id  AS observation_id,
                a.price_unavailable_since IS NOT NULL AS price_unavailable,
                COALESCE(a.excluded, false) AS excluded
            FROM balance_observation bo
            JOIN wallet w ON w.id = bo.wallet_id
            JOIN asset  a ON a.id = bo.asset_id
            WHERE w.status = 'active'
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
            observation_id=uuid.UUID(str(row[7])),
            token_address=row[2],
            raw_amount=int(row[3]),
            block_number=int(row[4]),
            block_time=row[5],
            decimals=int(row[6]),
            price_unavailable=bool(row[8]),
            excluded=bool(row[9]),
        )
        for row in result
    ]


async def _get_latest_prices(
    session: AsyncSession,
) -> tuple[dict[uuid.UUID, Decimal], set[uuid.UUID]]:
    """Return ({asset_id: price_usd}, {quote_set_id}) from the most recent complete quote_set."""
    result = await session.execute(
        sa.text(
            """
            SELECT qo.asset_id, qo.price_usd::text, qo.quote_set_id
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
    prices: dict[uuid.UUID, Decimal] = {}
    quote_set_ids: set[uuid.UUID] = set()
    for row in result:
        prices[uuid.UUID(str(row[0]))] = Decimal(str(row[1]))
        quote_set_ids.add(uuid.UUID(str(row[2])))
    return prices, quote_set_ids


def _compute_input_key(
    holdings: list[HoldingRow],
    quote_set_ids: set[uuid.UUID],
) -> str:
    """Deterministic hash of the exact inputs a snapshot was built from.

    The key is the sorted set of holdings' observation ids, the sorted set of
    currently-excluded asset ids, and the sorted set of quote_set ids the
    prices came from. Two publish calls with the same key always describe the
    same snapshot: the same holdings, priced from the same quote data, under
    the same exclusion set.

    The exclusion set is part of the key because it shapes the stored
    `quality` (AUD-448). Excluded assets are no longer dropped from the
    holdings list, so without this an exclusion toggle would leave the
    observation ids untouched, match the previous key, and return a snapshot
    whose quality was computed under the old exclusion set.
    """
    obs_part = ",".join(sorted(str(h.observation_id) for h in holdings))
    excl_part = ",".join(sorted({str(h.asset_id) for h in holdings if h.excluded}))
    qs_part = ",".join(sorted(str(qs_id) for qs_id in quote_set_ids))
    payload = f"obs:{obs_part}|excl:{excl_part}|qs:{qs_part}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


async def _get_snapshot_by_input_key(
    session: AsyncSession,
    input_key: str,
) -> SnapshotResult | None:
    """Return the already-published snapshot for this input key, if any."""
    result = await session.execute(
        sa.text(
            """
            SELECT
                vs.id,
                vs.quality,
                COUNT(vl.id) AS line_count,
                COUNT(vl.id) FILTER (WHERE vl.price_usd IS NOT NULL) AS priced_count
            FROM valuation_snapshot vs
            LEFT JOIN valuation_line vl ON vl.snapshot_id = vs.id
            WHERE vs.input_key = :input_key
            GROUP BY vs.id, vs.quality
            """
        ),
        {"input_key": input_key},
    )
    row = result.first()
    if row is None:
        return None
    return SnapshotResult(
        snapshot_id=uuid.UUID(str(row[0])),
        quality=str(row[1]),
        line_count=int(row[2]),
        priced_count=int(row[3]),
        created=False,
    )


def _compute_quality(
    holdings: list[HoldingRow],
    prices: dict[uuid.UUID, Decimal],
) -> tuple[str, int]:
    """Return (quality_string, priced_count).

    Quality:
      unknown  — no holdings
      stale    — holdings exist but no prices available at all
      partial  — some holdings have prices, but at least one has never been
                 asked about (AUD-361) — blocks total_usd, since that holding
                 could turn out to be priceable on the very next refresh
      gaps     — every unpriced holding has been asked about and the provider
                 confirmed it doesn't know it (AUD-361) — total_usd is still
                 computable from the holdings that do have a price
      complete — every holding has a price
    """
    if not holdings:
        return "unknown", 0

    priced = sum(1 for h in holdings if h.asset_id in prices)

    if priced == 0:
        return "stale", 0
    if priced == len(holdings):
        return "complete", priced

    never_asked = any(h.asset_id not in prices and not h.price_unavailable for h in holdings)
    return ("partial" if never_asked else "gaps"), priced
