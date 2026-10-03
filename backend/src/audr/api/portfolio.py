"""FastAPI route for GET /portfolio (AUD-317).

Returns the current portfolio envelope from the latest published
valuation snapshot, matching the PortfolioResponse contract shape.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import _require_session
from audr.assets.constants import is_native_eth
from audr.auth.models import Session
from audr.db import get_db
from audr.portfolio.money import format_decimal, quantity_to_usd, raw_to_quantity

router = APIRouter(prefix="/api/v1")

_SOURCE_TO_METADATA_SOURCE: dict[str, str] = {
    "catalog": "catalog",
    "manual": "owner",
    "chain": "chain",
}

# Worst-status precedence for aggregating per-line read_status into a single
# per-asset allocation row (AUD-404): error > stale > pending > ok.
_STATUS_RANK: dict[str, int] = {"ok": 0, "pending": 1, "stale": 2, "error": 3}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PortfolioQualityOut(BaseModel):
    incomplete: bool
    stale_balances: bool
    stale_prices: bool
    mixed_observation_times: bool
    discovery_overdue: bool
    verification_pending: bool
    invalidated: bool


class HoldingOut(BaseModel):
    wallet_id: str
    asset_id: str
    contract_address: str | None
    is_native: bool
    raw_balance: str | None
    decimals: int | None
    quantity: str | None
    price_usd: str | None
    value_usd: str | None
    included: bool
    metadata_source: str
    read_status: str
    block_time: str | None
    observed_at: str | None
    last_success_at: str | None
    logo_url: str | None


class AllocationItemOut(BaseModel):
    asset_id: str
    symbol: str
    value_usd: str | None
    percentage: str
    quantity: str | None
    price_usd: str | None
    wallet_count: int
    read_status: str
    included: bool
    logo_url: str | None = None


class PortfolioResponseOut(BaseModel):
    snapshot_id: str | None
    membership_revision: str | None
    valuation_time: str | None
    currency: str
    priced_subtotal_usd: str | None
    total_usd: str | None
    unpriced_asset_count: int
    quality: PortfolioQualityOut
    balance_block: int | None
    balance_block_time: str | None
    balance_observed_at: str | None
    discovery_completed_at: str | None
    holdings: list[HoldingOut]
    allocations: list[AllocationItemOut]
    stale_contribution_usd: str | None
    request_id: str
    generated_at: str


# ---------------------------------------------------------------------------
# Quality mapping
# ---------------------------------------------------------------------------


def _map_quality(quality_str: str, *, total_is_null: bool) -> PortfolioQualityOut:
    """Map the snapshot quality string to the contract quality object.

    ``incomplete`` tracks whether ``total_usd`` is actually reachable, not
    raw holding coverage (AUD-361): a "gaps" snapshot (every unpriced holding
    is one the provider confirmed it doesn't know, none are genuinely
    unknown) still yields a real total, so it is not reported as incomplete —
    only "partial"/"stale"/"unknown" snapshots, where total_usd really is
    null, are. ``stale_prices`` is true only for "stale" (zero holdings
    priced at all); coverage gaps are not price staleness, so a successful
    quote_refresh that leaves confirmed-unpriceable dust behind still reports
    fresh prices.
    """
    return PortfolioQualityOut(
        incomplete=total_is_null,
        stale_balances=quality_str == "stale",
        stale_prices=quality_str == "stale",
        mixed_observation_times=False,
        discovery_overdue=False,
        verification_pending=False,
        invalidated=False,
    )


def _icon_url(asset_id: str) -> str:
    """Build the icon proxy URL for *asset_id* (AUD-385).

    Emitted unconditionally — the icon cache is populated out-of-band by
    asset_icon_refresh, and the frontend's AssetEmblem already falls back to
    a monogram on a 404/image error, so there is no need to check
    resolvability here and no extra latency on this endpoint.
    """
    return f"/api/v1/assets/{asset_id}/icon"


_EMPTY_QUALITY = PortfolioQualityOut(
    incomplete=True,
    stale_balances=False,
    stale_prices=False,
    mixed_observation_times=False,
    discovery_overdue=False,
    verification_pending=False,
    invalidated=False,
)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/portfolio", response_model=PortfolioResponseOut)
async def get_portfolio(
    _session: Annotated[Session, Depends(_require_session)],
    db: AsyncSession = Depends(get_db),
    wallet_id: str | None = None,
) -> PortfolioResponseOut:
    """Return the current portfolio from the latest published valuation snapshot."""
    now = datetime.now(tz=UTC)

    # 1. Find the latest published valuation snapshot.
    snap_result = await db.execute(
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
    snap_row = snap_result.first()

    if snap_row is None:
        # No snapshot yet — return an empty but valid envelope.
        return PortfolioResponseOut(
            snapshot_id=None,
            membership_revision=None,
            valuation_time=None,
            currency="USD",
            priced_subtotal_usd=None,
            total_usd=None,
            unpriced_asset_count=0,
            quality=_EMPTY_QUALITY,
            balance_block=None,
            balance_block_time=None,
            balance_observed_at=None,
            discovery_completed_at=None,
            holdings=[],
            allocations=[],
            stale_contribution_usd=None,
            request_id=str(uuid.uuid4()),
            generated_at=now.isoformat(),
        )

    snapshot_id = str(snap_row[0])
    valuation_time = snap_row[1]
    quality_str = str(snap_row[2])

    # 2. Query valuation lines joined with asset and wallet info.
    wallet_filter = "AND vl.wallet_id = :wallet_id" if wallet_id else ""
    lines_result = await db.execute(
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
                COALESCE(a.decimals_override, a.decimals) AS decimals,
                a.source,
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
            """
        ),
        {"snap_id": snapshot_id, **({"wallet_id": wallet_id} if wallet_id else {})},
    )

    lines = [dict(r) for r in lines_result.mappings()]

    # 3. Determine the freshest balance block among this snapshot's holdings.
    # Every wallet/asset scanned in the same balance_scan run shares one
    # block_number (jobs.__main__.handle_balance_scan reads it once per run),
    # so a line whose block is behind the max was not updated by the latest
    # run and is a carried-forward (stale) balance — this is what
    # stale_contribution_usd below sums (AUD-72).
    max_block: int | None = None
    max_block_time: datetime | None = None
    for line in lines:
        block_number = line["block_number"]
        if block_number is not None:
            bn = int(block_number)
            if max_block is None or bn > max_block:
                max_block = bn
                max_block_time = line["block_time"]

    # 4. Build holdings and compute totals.
    holdings: list[HoldingOut] = []
    holding_is_stale: list[bool] = []
    priced_subtotal = Decimal(0)
    stale_subtotal = Decimal(0)
    max_observed: datetime | None = None

    for line in lines:
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
                pass

        price_usd_str: str | None = line["price_usd"]
        value_usd_str: str | None = line["value_usd"]

        if price_usd_str is not None and value_usd_str is not None:
            try:
                priced_subtotal += Decimal(value_usd_str)
            except Exception:
                pass

        block_number = line["block_number"]
        is_stale_balance = (
            block_number is not None and max_block is not None and int(block_number) < max_block
        )
        if is_stale_balance and price_usd_str is not None and value_usd_str is not None:
            try:
                stale_subtotal += Decimal(value_usd_str)
            except Exception:
                pass

        block_time_val = line["block_time"]
        block_time_str: str | None = (
            block_time_val.isoformat() if hasattr(block_time_val, "isoformat") else None
        )

        observed_at = line["observed_at"]
        observed_str: str | None = None
        if observed_at is not None:
            if hasattr(observed_at, "isoformat"):
                observed_str = observed_at.isoformat()
                if max_observed is None or observed_at > max_observed:
                    max_observed = observed_at
            else:
                observed_str = str(observed_at)

        holdings.append(
            HoldingOut(
                wallet_id=str(line["wallet_id"]),
                asset_id=str(line["asset_id"]),
                contract_address=None if is_native else token_address,
                is_native=is_native,
                raw_balance=raw_amount_str,
                decimals=decimals_val,
                quantity=quantity_str,
                price_usd=price_usd_str,
                value_usd=value_usd_str,
                included=True,
                metadata_source=metadata_source,
                read_status="ok",
                block_time=block_time_str,
                observed_at=observed_str,
                last_success_at=observed_str,
                logo_url=_icon_url(str(line["asset_id"])),
            )
        )
        holding_is_stale.append(is_stale_balance)

    # total_usd is null unless every unpriced holding has been confirmed
    # unpriceable by the provider ("gaps") — a holding that has simply never
    # been asked about yet ("partial") still blocks the total (AUD-361).
    total_usd_str: str | None = (
        format_decimal(priced_subtotal) if quality_str in ("complete", "gaps") else None
    )
    if not holdings:
        priced_subtotal_str: str | None = None
        stale_contribution_str: str | None = None
    else:
        priced_subtotal_str = format_decimal(priced_subtotal)
        stale_contribution_str = format_decimal(stale_subtotal)

    # 5. Aggregate allocations by asset_id across wallets (AUD-404). Two
    # wallets holding the same asset must collapse into one row — summing
    # value/quantity and tracking wallet_count/read_status/included across
    # the contributing lines — rather than one row per (wallet, asset).
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

    unpriced_asset_count = sum(
        1 for asset_id in asset_order if not asset_aggs[asset_id]["has_priced_line"]
    )

    allocations: list[AllocationItemOut] = []
    if priced_subtotal > 0:
        for asset_id in asset_order:
            agg = asset_aggs[asset_id]
            if not agg["has_priced_line"]:
                continue
            try:
                pct = (agg["value_usd"] / priced_subtotal * 100).quantize(Decimal("0.01"))
            except Exception:
                continue
            allocations.append(
                AllocationItemOut(
                    asset_id=asset_id,
                    symbol=agg["symbol"],
                    value_usd=format_decimal(agg["value_usd"]),
                    percentage=str(pct),
                    quantity=format_decimal(agg["quantity"]) if agg["quantity_known"] else None,
                    price_usd=agg["price_usd"],
                    wallet_count=len(agg["wallet_ids"]),
                    read_status=agg["status"],
                    included=agg["included"],
                    logo_url=_icon_url(asset_id),
                )
            )

    # Unpriced assets are surfaced in allocations too (sorted after the
    # priced ones) — dropping the Holdings page must not make them invisible.
    for asset_id in asset_order:
        agg = asset_aggs[asset_id]
        if agg["has_priced_line"]:
            continue
        allocations.append(
            AllocationItemOut(
                asset_id=asset_id,
                symbol=agg["symbol"],
                value_usd=None,
                percentage="0",
                quantity=format_decimal(agg["quantity"]) if agg["quantity_known"] else None,
                price_usd=None,
                wallet_count=len(agg["wallet_ids"]),
                read_status=agg["status"],
                included=agg["included"],
                logo_url=_icon_url(asset_id),
            )
        )

    quality = _map_quality(quality_str, total_is_null=total_usd_str is None)
    valuation_time_str: str | None = None
    if valuation_time is not None:
        valuation_time_str = (
            valuation_time.isoformat()
            if hasattr(valuation_time, "isoformat")
            else str(valuation_time)
        )
    balance_block_time_str: str | None = (
        max_block_time.isoformat() if hasattr(max_block_time, "isoformat") else None
    )

    return PortfolioResponseOut(
        snapshot_id=snapshot_id,
        membership_revision=None,
        valuation_time=valuation_time_str,
        currency="USD",
        priced_subtotal_usd=priced_subtotal_str,
        total_usd=total_usd_str,
        unpriced_asset_count=unpriced_asset_count,
        quality=quality,
        balance_block=max_block,
        balance_block_time=balance_block_time_str,
        balance_observed_at=max_observed.isoformat() if max_observed else None,
        discovery_completed_at=None,
        holdings=holdings,
        allocations=allocations,
        stale_contribution_usd=stale_contribution_str,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )
