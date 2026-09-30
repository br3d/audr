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
from audr.auth.models import Session
from audr.db import get_db
from audr.portfolio.money import format_decimal, quantity_to_usd, raw_to_quantity

router = APIRouter(prefix="/api/v1")

_NATIVE_ETH_ADDRESS = "0x0000000000000000000000000000000000000000"

_SOURCE_TO_METADATA_SOURCE: dict[str, str] = {
    "catalog": "catalog",
    "manual": "owner",
    "chain": "chain",
}


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


class AllocationItemOut(BaseModel):
    asset_id: str
    symbol: str
    value_usd: str
    percentage: str


class PortfolioResponseOut(BaseModel):
    snapshot_id: str | None
    membership_revision: str | None
    valuation_time: str | None
    currency: str
    priced_subtotal_usd: str | None
    total_usd: str | None
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


def _map_quality(quality_str: str) -> PortfolioQualityOut:
    """Map the snapshot quality string to the contract quality object."""
    return PortfolioQualityOut(
        incomplete=quality_str in ("partial", "unknown"),
        stale_balances=quality_str == "stale",
        stale_prices=quality_str in ("stale", "partial"),
        mixed_observation_times=False,
        discovery_overdue=False,
        verification_pending=False,
        invalidated=False,
    )


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

    # 3. Build holdings and compute totals.
    holdings: list[HoldingOut] = []
    priced_subtotal = Decimal(0)
    all_have_price = True
    max_block: int | None = None
    max_observed: datetime | None = None

    for line in lines:
        token_address = str(line["token_address"])
        is_native = token_address == _NATIVE_ETH_ADDRESS
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
        else:
            all_have_price = False

        block_number = line["block_number"]
        if block_number is not None:
            bn = int(block_number)
            if max_block is None or bn > max_block:
                max_block = bn

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
                block_time=None,
                observed_at=observed_str,
                last_success_at=observed_str,
            )
        )

    # total_usd is null when any included holding lacks a price.
    total_usd_str: str | None = format_decimal(priced_subtotal) if all_have_price and holdings else None
    if not holdings:
        priced_subtotal_str: str | None = None
    else:
        priced_subtotal_str = format_decimal(priced_subtotal)

    # 4. Build allocations from included priced holdings.
    allocations: list[AllocationItemOut] = []
    if priced_subtotal > 0:
        for holding in holdings:
            if holding.value_usd is not None:
                try:
                    val = Decimal(holding.value_usd)
                    pct = (val / priced_subtotal * 100).quantize(Decimal("0.01"))
                    # Look up symbol from the line.
                    matching = next(
                        (ln for ln in lines if str(ln["asset_id"]) == holding.asset_id),
                        None,
                    )
                    symbol = str(matching["symbol"]) if matching else "?"
                    allocations.append(
                        AllocationItemOut(
                            asset_id=holding.asset_id,
                            symbol=symbol,
                            value_usd=holding.value_usd,
                            percentage=str(pct),
                        )
                    )
                except Exception:
                    pass

    quality = _map_quality(quality_str)
    valuation_time_str: str | None = None
    if valuation_time is not None:
        valuation_time_str = (
            valuation_time.isoformat()
            if hasattr(valuation_time, "isoformat")
            else str(valuation_time)
        )

    return PortfolioResponseOut(
        snapshot_id=snapshot_id,
        membership_revision=None,
        valuation_time=valuation_time_str,
        currency="USD",
        priced_subtotal_usd=priced_subtotal_str,
        total_usd=total_usd_str,
        quality=quality,
        balance_block=max_block,
        balance_block_time=None,
        balance_observed_at=max_observed.isoformat() if max_observed else None,
        discovery_completed_at=None,
        holdings=holdings,
        allocations=allocations,
        stale_contribution_usd=None,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )
