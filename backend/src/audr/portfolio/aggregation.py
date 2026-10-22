"""Portfolio aggregation for GET /portfolio (AUD-692).

Owns the snapshot lookup, holdings assembly, allocation math, 24h price
delta, and balance-staleness computation that used to live inline in
api/portfolio.py. Returns typed, presentation-agnostic view objects; the
route is responsible only for request parsing and DTO mapping.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.constants import is_native_eth
from audr.portfolio.money import format_decimal, raw_to_quantity

logger = logging.getLogger(__name__)

_SOURCE_TO_METADATA_SOURCE: dict[str, str] = {
    "catalog": "catalog",
    "manual": "owner",
    "chain": "chain",
}

# Worst-status precedence for aggregating per-line read_status into a single
# per-asset allocation row (AUD-404): error > stale > pending > ok.
_STATUS_RANK: dict[str, int] = {"ok": 0, "pending": 1, "stale": 2, "error": 3}


@dataclass
class HoldingView:
    wallet_id: str
    asset_id: str
    token_address: str
    is_native: bool
    raw_balance: str | None
    decimals: int
    quantity: str | None
    price_usd: str | None
    value_usd: str | None
    included: bool
    metadata_source: str
    block_time: datetime | None
    observed_at: datetime | None


@dataclass
class AllocationView:
    asset_id: str
    symbol: str
    name: str
    value_usd: str | None
    percentage: str
    quantity: str | None
    price_usd: str | None
    price_change_24h_pct: str | None
    wallet_count: int
    read_status: str
    included: bool


@dataclass
class PortfolioView:
    snapshot_id: str | None
    valuation_time: datetime | None
    quality: str
    priced_subtotal_usd: str | None
    total_usd: str | None
    unpriced_asset_count: int
    balance_block: int | None
    balance_block_time: datetime | None
    balance_observed_at: datetime | None
    stale_contribution_usd: str | None
    holdings: list[HoldingView] = field(default_factory=list)
    allocations: list[AllocationView] = field(default_factory=list)


_EMPTY_VIEW = PortfolioView(
    snapshot_id=None,
    valuation_time=None,
    quality="unknown",
    priced_subtotal_usd=None,
    total_usd=None,
    unpriced_asset_count=0,
    balance_block=None,
    balance_block_time=None,
    balance_observed_at=None,
    stale_contribution_usd=None,
)


async def build_portfolio_view(
    session: AsyncSession,
    *,
    wallet_id: str | None,
    now: datetime,
) -> PortfolioView:
    """Return the current portfolio from the latest published valuation snapshot."""
    snap_row = await _get_latest_published_snapshot(session)
    if snap_row is None:
        return _EMPTY_VIEW

    snapshot_id = str(snap_row[0])
    valuation_time = snap_row[1]
    quality_str = str(snap_row[2])

    lines = await _get_snapshot_lines(session, snapshot_id=snapshot_id, wallet_id=wallet_id)

    max_block, max_block_time = _compute_max_block(lines)

    holdings: list[HoldingView] = []
    holding_is_stale: list[bool] = []
    priced_subtotal = Decimal(0)
    stale_subtotal = Decimal(0)
    max_observed: datetime | None = None

    for line in lines:
        is_included = not bool(line["excluded"])
        token_address = str(line["token_address"])
        is_native = is_native_eth(token_address)
        source = str(line["source"])
        metadata_source = _SOURCE_TO_METADATA_SOURCE.get(source, "owner")

        raw_amount_str: str | None = line["raw_amount"]
        decimals_val = int(line["decimals"])

        quantity_str: str | None = None
        if raw_amount_str is not None:
            try:
                qty = raw_to_quantity(int(raw_amount_str), decimals_val)
                quantity_str = format_decimal(qty)
            except Exception:
                logger.warning(
                    "Failed to compute quantity for asset %s (raw_amount=%r, decimals=%r)",
                    token_address,
                    raw_amount_str,
                    decimals_val,
                    exc_info=True,
                )

        price_usd_str: str | None = line["price_usd"]
        value_usd_str: str | None = line["value_usd"]

        if is_included and price_usd_str is not None and value_usd_str is not None:
            try:
                priced_subtotal += Decimal(value_usd_str)
            except Exception:
                logger.warning(
                    "Failed to parse value_usd %r for asset %s; excluded from priced_subtotal",
                    value_usd_str,
                    token_address,
                    exc_info=True,
                )

        block_number = line["block_number"]
        is_stale_balance = (
            block_number is not None and max_block is not None and int(block_number) < max_block
        )
        if (
            is_included
            and is_stale_balance
            and price_usd_str is not None
            and value_usd_str is not None
        ):
            try:
                stale_subtotal += Decimal(value_usd_str)
            except Exception:
                logger.warning(
                    "Failed to parse value_usd %r for asset %s; excluded from stale_subtotal",
                    value_usd_str,
                    token_address,
                    exc_info=True,
                )

        block_time_val = line["block_time"]
        block_time = block_time_val if hasattr(block_time_val, "isoformat") else None

        observed_at = line["observed_at"]
        if observed_at is not None and hasattr(observed_at, "isoformat"):
            if max_observed is None or observed_at > max_observed:
                max_observed = observed_at
        else:
            observed_at = None

        holdings.append(
            HoldingView(
                wallet_id=str(line["wallet_id"]),
                asset_id=str(line["asset_id"]),
                token_address=token_address,
                is_native=is_native,
                raw_balance=raw_amount_str,
                decimals=decimals_val,
                quantity=quantity_str,
                price_usd=price_usd_str,
                value_usd=value_usd_str,
                included=is_included,
                metadata_source=metadata_source,
                block_time=block_time,
                observed_at=observed_at,
            )
        )
        holding_is_stale.append(is_stale_balance)

    # total_usd is null unless every unpriced holding has been confirmed
    # unpriceable by the provider ("gaps") — a holding that has simply never
    # been asked about yet ("partial") still blocks the total (AUD-361).
    total_usd_str: str | None = (
        format_decimal(priced_subtotal) if quality_str in ("complete", "gaps") else None
    )
    # "No subtotal" means nothing counts towards one. Excluded rows are in
    # `holdings` now (AUD-448), so an all-excluded portfolio must still report
    # a null subtotal rather than a misleading $0.00.
    if not any(h.included for h in holdings):
        priced_subtotal_str: str | None = None
        stale_contribution_str: str | None = None
    else:
        priced_subtotal_str = format_decimal(priced_subtotal)
        stale_contribution_str = format_decimal(stale_subtotal)

    asset_order, asset_aggs = _aggregate_by_asset(lines, holdings, holding_is_stale)

    # Only included assets can leave the total incomplete — an excluded
    # unpriced token is not a gap in a figure it never contributed to.
    unpriced_asset_count = sum(
        1
        for asset_id in asset_order
        if asset_aggs[asset_id]["included"] and not asset_aggs[asset_id]["has_priced_line"]
    )

    # price_change_24h_pct reuses the same current price already selected
    # above for price_usd — one query covering every priced asset, no N+1.
    current_prices = {
        asset_id: asset_aggs[asset_id]["price_usd"]
        for asset_id in asset_order
        if asset_aggs[asset_id]["price_usd"] is not None
    }
    price_change_pcts = await _get_price_change_24h_pct(session, current_prices, now)

    allocations = _build_allocations(asset_order, asset_aggs, priced_subtotal, price_change_pcts)

    balance_block_time = max_block_time if hasattr(max_block_time, "isoformat") else None

    return PortfolioView(
        snapshot_id=snapshot_id,
        valuation_time=valuation_time,
        quality=quality_str,
        priced_subtotal_usd=priced_subtotal_str,
        total_usd=total_usd_str,
        unpriced_asset_count=unpriced_asset_count,
        balance_block=max_block,
        balance_block_time=balance_block_time,
        balance_observed_at=max_observed,
        stale_contribution_usd=stale_contribution_str,
        holdings=holdings,
        allocations=allocations,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_latest_published_snapshot(session: AsyncSession) -> sa.Row | None:
    """Return (id, snapshotted_at, quality) for the latest published snapshot, or None."""
    result = await session.execute(
        sa.text(
            """
            SELECT id, snapshotted_at, quality
            FROM valuation_snapshot
            WHERE published_at IS NOT NULL
            ORDER BY published_at DESC
            LIMIT 1
            """
        )
    )
    return result.first()


async def _get_snapshot_lines(
    session: AsyncSession,
    *,
    snapshot_id: str,
    wallet_id: str | None,
) -> list[dict]:
    """Return valuation lines for *snapshot_id*, joined with asset and balance metadata.

    Exclusion is applied at read time against the asset's *current*
    `excluded` flag (AUD-447), but the excluded rows are still returned,
    flagged `included=false` (AUD-448). They are omitted from every total —
    priced_subtotal, total_usd, the allocation percentages and the unpriced
    count — so Exclude takes full effect on the next refetch, while the
    client keeps the row's value and can therefore redraw instantly in
    *both* directions: dropping a row needs no round trip, and putting it
    back needs no re-scan to recover a number it never lost.
    """
    wallet_filter = "AND vl.wallet_id = :wallet_id" if wallet_id else ""
    result = await session.execute(
        sa.text(
            f"""
            SELECT
                vl.wallet_id,
                vl.asset_id,
                vl.raw_amount::text   AS raw_amount,
                vl.block_number,
                vl.block_time         AS block_time,
                vl.price_usd::text    AS price_usd,
                vl.value_usd::text    AS value_usd,
                a.token_address,
                a.symbol,
                a.name,
                COALESCE(a.decimals_override, a.decimals) AS decimals,
                a.source,
                COALESCE(a.excluded, false) AS excluded,
                bo.observed_at        AS observed_at
            FROM valuation_line vl
            JOIN asset a ON a.id = vl.asset_id
            LEFT JOIN LATERAL (
                SELECT bo2.observed_at
                FROM balance_observation bo2
                WHERE bo2.id = vl.observation_id
            ) bo ON true
            WHERE vl.snapshot_id = :snap_id
              {wallet_filter}
            ORDER BY vl.value_usd DESC NULLS LAST
            """  # noqa: S608 -- wallet_filter is a fixed ":param" fragment; the value is bound, never interpolated
        ),
        {"snap_id": snapshot_id, **({"wallet_id": wallet_id} if wallet_id else {})},
    )
    return [dict(r) for r in result.mappings()]


def _compute_max_block(lines: list[dict]) -> tuple[int | None, datetime | None]:
    """Return the freshest balance block among this snapshot's holdings.

    Every wallet/asset scanned in the same balance_scan run shares one
    block_number (jobs.__main__.handle_balance_scan reads it once per run),
    so a line whose block is behind the max was not updated by the latest
    run and is a carried-forward (stale) balance — this is what
    stale_contribution_usd sums (AUD-72).

    Excluded lines are skipped: `balance_block` describes the portfolio the
    owner actually holds, and letting an excluded asset set the baseline
    would let it mark every real holding Stale (AUD-448).
    """
    max_block: int | None = None
    max_block_time: datetime | None = None
    for line in lines:
        if line["excluded"]:
            continue
        block_number = line["block_number"]
        if block_number is not None:
            bn = int(block_number)
            if max_block is None or bn > max_block:
                max_block = bn
                max_block_time = line["block_time"]
    return max_block, max_block_time


def _aggregate_by_asset(
    lines: list[dict],
    holdings: list[HoldingView],
    holding_is_stale: list[bool],
) -> tuple[list[str], dict[str, dict]]:
    """Aggregate per-line holdings by asset_id across wallets (AUD-404).

    Two wallets holding the same asset must collapse into one row — summing
    value/quantity and tracking wallet_count/read_status/included across the
    contributing lines — rather than one row per (wallet, asset).
    """
    asset_order: list[str] = []
    asset_aggs: dict[str, dict] = {}

    for holding, is_stale in zip(holdings, holding_is_stale, strict=True):
        agg = asset_aggs.get(holding.asset_id)
        if agg is None:
            matching = next(
                (ln for ln in lines if str(ln["asset_id"]) == holding.asset_id),
                None,
            )
            agg = {
                "symbol": str(matching["symbol"]) if matching else "?",
                "name": str(matching["name"]) if matching else "?",
                "wallet_ids": set(),
                "value_usd": Decimal(0),
                "has_priced_line": False,
                "quantity": Decimal(0),
                "quantity_known": True,
                "price_usd": None,
                "status": "ok",
                "included": False,
            }
            asset_aggs[holding.asset_id] = agg
            asset_order.append(holding.asset_id)

        agg["wallet_ids"].add(holding.wallet_id)
        if holding.included:
            agg["included"] = True

        if holding.quantity is not None and agg["quantity_known"]:
            agg["quantity"] += Decimal(holding.quantity)
        else:
            agg["quantity_known"] = False

        if holding.value_usd is not None:
            agg["has_priced_line"] = True
            agg["value_usd"] += Decimal(holding.value_usd)
            if agg["price_usd"] is None and holding.price_usd is not None:
                agg["price_usd"] = holding.price_usd

        line_status = "stale" if is_stale else "ok"
        if _STATUS_RANK[line_status] > _STATUS_RANK[agg["status"]]:
            agg["status"] = line_status

    return asset_order, asset_aggs


def _build_allocations(
    asset_order: list[str],
    asset_aggs: dict[str, dict],
    priced_subtotal: Decimal,
    price_change_pcts: dict[str, str],
) -> list[AllocationView]:
    """Build the allocation rows: excluded first, then priced-included, then unpriced.

    Excluded rows are emitted with their value but a 0% share, and
    unconditionally — the `priced_subtotal > 0` guard below only governs
    whether a *share* is computable, and an all-excluded portfolio has a
    zero subtotal yet must still hand the client the rows it needs to put
    an asset back (AUD-448).
    """
    allocations: list[AllocationView] = []
    for asset_id in asset_order:
        agg = asset_aggs[asset_id]
        if not agg["has_priced_line"]:
            continue
        if not agg["included"]:
            allocations.append(
                AllocationView(
                    asset_id=asset_id,
                    symbol=agg["symbol"],
                    name=agg["name"],
                    value_usd=format_decimal(agg["value_usd"]),
                    percentage="0",
                    quantity=format_decimal(agg["quantity"]) if agg["quantity_known"] else None,
                    price_usd=agg["price_usd"],
                    price_change_24h_pct=price_change_pcts.get(asset_id),
                    wallet_count=len(agg["wallet_ids"]),
                    read_status=agg["status"],
                    included=False,
                )
            )
            continue
        if priced_subtotal <= 0:
            continue
        try:
            pct = (agg["value_usd"] / priced_subtotal * 100).quantize(Decimal("0.01"))
        except Exception:
            logger.warning(
                "Failed to compute allocation percentage for asset %s; omitted",
                asset_id,
                exc_info=True,
            )
            continue
        allocations.append(
            AllocationView(
                asset_id=asset_id,
                symbol=agg["symbol"],
                name=agg["name"],
                value_usd=format_decimal(agg["value_usd"]),
                percentage=str(pct),
                quantity=format_decimal(agg["quantity"]) if agg["quantity_known"] else None,
                price_usd=agg["price_usd"],
                price_change_24h_pct=price_change_pcts.get(asset_id),
                wallet_count=len(agg["wallet_ids"]),
                read_status=agg["status"],
                included=True,
            )
        )

    # Unpriced assets are surfaced in allocations too (sorted after the
    # priced ones) — dropping the Holdings page must not make them invisible.
    for asset_id in asset_order:
        agg = asset_aggs[asset_id]
        if agg["has_priced_line"]:
            continue
        allocations.append(
            AllocationView(
                asset_id=asset_id,
                symbol=agg["symbol"],
                name=agg["name"],
                value_usd=None,
                percentage="0",
                quantity=format_decimal(agg["quantity"]) if agg["quantity_known"] else None,
                price_usd=None,
                price_change_24h_pct=None,
                wallet_count=len(agg["wallet_ids"]),
                read_status=agg["status"],
                included=agg["included"],
            )
        )

    return allocations


async def _get_price_change_24h_pct(
    session: AsyncSession,
    current_prices: dict[str, str],
    now: datetime,
) -> dict[str, str]:
    """Return {asset_id: price_change_24h_pct} for assets with a current price.

    The reference price is the quote_observation closest to now()-24h, within
    a ±6h tolerance window — one query (DISTINCT ON) covering every asset, no
    N+1. Assets with no reference observation in the window, or whose
    reference price is zero, are omitted (unknown ≠ zero; caller treats a
    missing key as null).
    """
    asset_ids = list(current_prices)
    if not asset_ids:
        return {}

    target = now - timedelta(hours=24)
    window_start = now - timedelta(hours=30)
    window_end = now - timedelta(hours=18)

    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT ON (qo.asset_id)
                qo.asset_id,
                qo.price_usd::text AS ref_price_usd
            FROM quote_observation qo
            JOIN quote_set qs ON qs.id = qo.quote_set_id
            WHERE qo.asset_id = ANY(:asset_ids ::uuid[])
              AND qs.status = 'complete'
              AND qs.fetched_at BETWEEN :window_start AND :window_end
            ORDER BY qo.asset_id, ABS(EXTRACT(EPOCH FROM (qs.fetched_at - :target)))
            """
        ),
        {
            "asset_ids": asset_ids,
            "window_start": window_start,
            "window_end": window_end,
            "target": target,
        },
    )

    pct_by_asset: dict[str, str] = {}
    for row in result:
        asset_id = str(row[0])
        ref_price = Decimal(row[1])
        if ref_price == 0:
            continue
        current_price = Decimal(current_prices[asset_id])
        pct = ((current_price - ref_price) / ref_price * 100).quantize(Decimal("0.01"))
        pct_by_asset[asset_id] = str(pct)
    return pct_by_asset
