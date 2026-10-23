"""FastAPI route for GET /portfolio (AUD-317).

Returns the current portfolio envelope from the latest published
valuation snapshot, matching the PortfolioResponse contract shape. The
snapshot lookup, holdings assembly, allocation math, and 24h price delta
live in audr.portfolio.aggregation; this module only parses the request
and maps the resulting view onto the response DTO.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import ReadPrincipal, _require_read_auth
from audr.db import get_db
from audr.portfolio.aggregation import (
    AllocationView,
    HoldingView,
    PortfolioView,
    build_portfolio_view,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1")


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
    name: str
    value_usd: str | None
    percentage: str
    quantity: str | None
    price_usd: str | None
    price_change_24h_pct: str | None = None
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


def _holding_out(holding: HoldingView) -> HoldingOut:
    """Map a HoldingView onto the contract shape.

    ``read_status`` is unconditionally "ok" here — per-line staleness only
    ever surfaces through the aggregated allocation row's read_status, never
    on the individual holding (existing contract behaviour, unchanged by
    AUD-692).
    """
    observed_str = holding.observed_at.isoformat() if holding.observed_at else None
    return HoldingOut(
        wallet_id=holding.wallet_id,
        asset_id=holding.asset_id,
        contract_address=None if holding.is_native else holding.token_address,
        is_native=holding.is_native,
        raw_balance=holding.raw_balance,
        decimals=holding.decimals,
        quantity=holding.quantity,
        price_usd=holding.price_usd,
        value_usd=holding.value_usd,
        included=holding.included,
        metadata_source=holding.metadata_source,
        read_status="ok",
        block_time=holding.block_time.isoformat() if holding.block_time else None,
        observed_at=observed_str,
        last_success_at=observed_str,
        logo_url=_icon_url(holding.asset_id),
    )


def _allocation_out(allocation: AllocationView) -> AllocationItemOut:
    return AllocationItemOut(
        asset_id=allocation.asset_id,
        symbol=allocation.symbol,
        name=allocation.name,
        value_usd=allocation.value_usd,
        percentage=allocation.percentage,
        quantity=allocation.quantity,
        price_usd=allocation.price_usd,
        price_change_24h_pct=allocation.price_change_24h_pct,
        wallet_count=allocation.wallet_count,
        read_status=allocation.read_status,
        included=allocation.included,
        logo_url=_icon_url(allocation.asset_id),
    )


def _to_response(view: PortfolioView, *, now: datetime) -> PortfolioResponseOut:
    quality = _map_quality(view.quality, total_is_null=view.total_usd is None)
    return PortfolioResponseOut(
        snapshot_id=view.snapshot_id,
        membership_revision=None,
        valuation_time=view.valuation_time.isoformat() if view.valuation_time else None,
        currency="USD",
        priced_subtotal_usd=view.priced_subtotal_usd,
        total_usd=view.total_usd,
        unpriced_asset_count=view.unpriced_asset_count,
        quality=quality,
        balance_block=view.balance_block,
        balance_block_time=(
            view.balance_block_time.isoformat() if view.balance_block_time else None
        ),
        balance_observed_at=(
            view.balance_observed_at.isoformat() if view.balance_observed_at else None
        ),
        discovery_completed_at=None,
        holdings=[_holding_out(h) for h in view.holdings],
        allocations=[_allocation_out(a) for a in view.allocations],
        stale_contribution_usd=view.stale_contribution_usd,
        request_id=str(uuid.uuid4()),
        generated_at=now.isoformat(),
    )


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.get("/portfolio", response_model=PortfolioResponseOut)
async def get_portfolio(
    _auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
    wallet_id: str | None = None,
) -> PortfolioResponseOut:
    """Return the current portfolio from the latest published valuation snapshot."""
    now = datetime.now(tz=UTC)
    view = await build_portfolio_view(db, wallet_id=wallet_id, now=now)
    return _to_response(view, now=now)
