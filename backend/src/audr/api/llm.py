"""FastAPI routes for a compact, context-window-shaped read surface (AUD-630).

GET /api/v1/llm/context
  One snapshot of the whole portfolio: total_usd, priced subtotal, quality
  flags, top-N allocations, 24h/7d/30d deltas, the newest headlines, unread
  alerts, and per-subsystem freshness.

GET /api/v1/llm/asset/{ref}
  One asset, addressable by symbol, contract address, or UUID: quantity,
  value, price, portfolio share, a downsampled 30-day series, its news, its
  alerts.

Both routes compute nothing new and call no provider: they project the
latest published valuation snapshot (via the same `get_portfolio()` used by
GET /portfolio), the history_point table, and the asset_news/news_alert
caches. `null` is never coerced to zero, every response carries `as_of` and a
plain-English `caveats` array, and `Accept: text/markdown` is honoured
alongside the default JSON.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import status as http_status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from audr.api.auth import ReadPrincipal, _require_read_auth
from audr.api.portfolio import AllocationItemOut, get_portfolio
from audr.assets.constants import is_native_eth
from audr.db import get_db

router = APIRouter(prefix="/api/v1/llm")

# Caps that keep GET /context within the ~2k-token budget the design doc
# (docs/llm-access.md §3) sets, regardless of portfolio size. Anything cut by
# a cap is named in `caveats`, never silently dropped.
_TOP_ALLOCATIONS_LIMIT = 8
_HEADLINES_LIMIT = 5
_ALERTS_LIMIT = 5

_ASSET_NEWS_LIMIT = 5
_ASSET_ALERTS_LIMIT = 5
_ASSET_SERIES_WINDOW_DAYS = 30
_ASSET_SERIES_CAP = 30

_STALE_PRICE_LAG_NOTE = (
    "Figures reflect the last published snapshot or cache, not a live read of "
    "the chain or any provider; they can lag real-time state."
)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class PortfolioBlockOut(BaseModel):
    as_of: str | None = Field(
        description=(
            "RFC3339 timestamp of the valuation snapshot these figures come from, "
            "or null if no snapshot has ever been published — in which case every "
            "other field in this block is also null."
        )
    )
    total_usd: str | None = Field(
        description=(
            "Exact decimal string, never a JSON float. Null means at least one "
            "held asset is unpriced or no snapshot exists yet — null is 'unknown', "
            "never zero."
        )
    )
    priced_subtotal_usd: str | None = Field(
        description=(
            "Sum of only the assets that do have a price, as an exact decimal "
            "string. Useful when total_usd is null: this is the best lower bound "
            "available. Null under the same conditions as total_usd."
        )
    )
    unpriced_asset_count: int = Field(
        description="Count of held assets with no price, the reason total_usd can be null."
    )
    incomplete: bool = Field(
        description="True if the snapshot omits at least one wallet or asset that should be in it."
    )
    stale_balances: bool = Field(
        description="True if at least one balance was carried forward from an earlier scan."
    )
    stale_prices: bool = Field(
        description="True if no fresh price quote has been fetched recently."
    )


class TopAllocationOut(BaseModel):
    asset_id: str = Field(
        description="Internal UUID of the asset; also valid as an /llm/asset/{ref}."
    )
    symbol: str = Field(description="Ticker symbol; also valid as an /llm/asset/{ref}.")
    name: str = Field(description="Human-readable asset name.")
    value_usd: str | None = Field(
        description="Exact decimal string. Null means this asset is unpriced, not worth zero."
    )
    percentage: str | None = Field(
        description="Share of total_usd, exact decimal string. Null whenever value_usd is null."
    )
    quantity: str | None = Field(
        description="Exact decimal string quantity held. Null only if the balance is unknown."
    )
    price_usd: str | None = Field(
        description="Exact decimal string unit price. Null means no price is currently available."
    )


class DeltasOut(BaseModel):
    pct_24h: str | None = Field(
        description=(
            "Percent change in total_usd over the last 24h, as an exact decimal "
            "string. Null means no historical snapshot was close enough in time "
            "to compute it — not a 0% change."
        )
    )
    pct_7d: str | None = Field(
        description="Percent change over 7 days. Null means it could not be computed, not 0%."
    )
    pct_30d: str | None = Field(
        description="Percent change over 30 days. Null means it could not be computed, not 0%."
    )


class HeadlineOut(BaseModel):
    asset_symbol: str = Field(description="Ticker symbol of the asset this headline is about.")
    title: str = Field(description="Headline title, as cached from the source site.")
    url: str = Field(description="Source URL of the article.")
    news_site: str = Field(description="Publisher name.")
    published_at: str = Field(description="RFC3339 timestamp the source reported for this article.")


class AlertSummaryOut(BaseModel):
    asset_symbol: str = Field(description="Ticker symbol of the asset this alert is about.")
    severity: str = Field(description="Alert severity, e.g. info/warning/critical.")
    summary: str = Field(description="Plain-English summary of what triggered the alert.")
    created_at: str = Field(description="RFC3339 timestamp the alert was generated.")
    read_at: str | None = Field(
        description="RFC3339 timestamp the owner marked this alert read, or null if still unread."
    )


class AlertsBlockOut(BaseModel):
    as_of: str | None = Field(
        description="RFC3339 timestamp of the most recent alert, or null if there are none."
    )
    unread_count: int = Field(description="Count of alerts across all assets with read_at null.")
    items: list[AlertSummaryOut] = Field(
        description="Most recent alerts, unread first; capped — see the caveats array if truncated."
    )


class FreshnessOut(BaseModel):
    balances_as_of: str | None = Field(
        description="RFC3339 timestamp of the last balance scan, or null if none has ever run."
    )
    quotes_as_of: str | None = Field(
        description="RFC3339 timestamp of the last completed price refresh, or null if never run."
    )
    news_as_of: str | None = Field(
        description="RFC3339 timestamp of the newest cached news, or null if the cache is empty."
    )
    alerts_as_of: str | None = Field(
        description="RFC3339 timestamp of the most recent alert, or null if none have ever fired."
    )


class LlmContextResponse(BaseModel):
    as_of: str = Field(
        description="RFC3339 timestamp this response was generated — not when the data changed."
    )
    currency: str = Field(description="ISO currency code every *_usd field is denominated in.")
    portfolio: PortfolioBlockOut = Field(
        description="Whole-portfolio totals and data-quality flags."
    )
    top_allocations: list[TopAllocationOut] = Field(
        description="Largest holdings by value_usd, descending. Capped — see caveats if truncated."
    )
    deltas: DeltasOut = Field(
        description="Percent change in total_usd over three lookback windows."
    )
    headlines: list[HeadlineOut] = Field(
        description="Most recent cached news across all held assets."
    )
    alerts: AlertsBlockOut = Field(description="Unread-first summary of recent news alerts.")
    freshness: FreshnessOut = Field(
        description="Per-subsystem timestamp of the last successful refresh."
    )
    caveats: list[str] = Field(
        description=(
            "Plain-English warnings about this specific response — e.g. stale "
            "balances, truncated lists, or why a figure is null. Always read "
            "this before stating any figure above as fact."
        )
    )
    request_id: str = Field(
        description="Opaque identifier for this response, for support correlation only."
    )


class AssetHoldingOut(BaseModel):
    quantity: str | None = Field(
        description="Exact decimal string quantity held across all wallets. Null means unknown."
    )
    value_usd: str | None = Field(
        description="Exact decimal string. Null means this asset is unpriced, not worth zero."
    )
    price_usd: str | None = Field(
        description="Exact decimal string unit price. Null means no price is currently available."
    )
    portfolio_share_pct: str | None = Field(
        description="Share of the whole portfolio's total_usd. Null whenever value_usd is null."
    )
    wallet_count: int = Field(description="Number of distinct wallets holding this asset.")
    included: bool = Field(description="Whether this holding counts toward the portfolio total.")


class AssetSeriesPointOut(BaseModel):
    as_of: str = Field(description="RFC3339 timestamp this historical snapshot was taken.")
    value_usd: str | None = Field(
        description="Exact decimal string. Null means this asset was unpriced at that time."
    )
    price_usd: str | None = Field(
        description="Exact decimal string unit price at that point in time, or null if unpriced."
    )


class AssetNewsItemOut(BaseModel):
    title: str = Field(description="Headline title, as cached from the source site.")
    url: str = Field(description="Source URL of the article.")
    news_site: str = Field(description="Publisher name.")
    published_at: str = Field(description="RFC3339 timestamp the source reported for this article.")


class AssetAlertItemOut(BaseModel):
    severity: str = Field(description="Alert severity, e.g. info/warning/critical.")
    summary: str = Field(description="Plain-English summary of what triggered the alert.")
    created_at: str = Field(description="RFC3339 timestamp the alert was generated.")
    read_at: str | None = Field(
        description="RFC3339 timestamp the owner marked this alert read, or null if still unread."
    )


class AssetAlertsBlockOut(BaseModel):
    unread_count: int = Field(description="Count of this asset's alerts with read_at null.")
    items: list[AssetAlertItemOut] = Field(
        description="This asset's recent alerts, unread first; capped — see caveats if truncated."
    )


class LlmAssetResponse(BaseModel):
    as_of: str = Field(
        description="RFC3339 timestamp this response was generated — not when the data changed."
    )
    asset_id: str = Field(description="Internal UUID of the resolved asset.")
    symbol: str = Field(description="Ticker symbol of the resolved asset.")
    name: str = Field(description="Human-readable name of the resolved asset.")
    contract_address: str | None = Field(
        description="ERC-20 contract address, lowercase. Null for native ETH, which has none."
    )
    is_native: bool = Field(description="True for native ETH, false for any ERC-20 token.")
    holding: AssetHoldingOut | None = Field(
        description=(
            "This asset's quantity, value and price. Null means the asset is not "
            "currently held in any tracked wallet — unknown, not a zero balance."
        )
    )
    series_30d: list[AssetSeriesPointOut] = Field(
        description="Value/price over the last 30 days, downsampled; see caveats if thinned."
    )
    news: list[AssetNewsItemOut] = Field(description="Most recent cached news for this asset.")
    alerts: AssetAlertsBlockOut = Field(
        description="Unread-first summary of this asset's news alerts."
    )
    caveats: list[str] = Field(
        description=(
            "Plain-English warnings about this specific response — e.g. that "
            "this asset isn't held, or why a figure is null. Always read this "
            "before stating any figure above as fact."
        )
    )
    request_id: str = Field(
        description="Opaque identifier for this response, for support correlation only."
    )


# ---------------------------------------------------------------------------
# Helpers — content negotiation
# ---------------------------------------------------------------------------


def _wants_markdown(request: Request) -> bool:
    return "text/markdown" in request.headers.get("accept", "").lower()


def _render(body: BaseModel, markdown: str, request: Request) -> Response:
    if _wants_markdown(request):
        return Response(content=markdown, media_type="text/markdown")
    return Response(content=body.model_dump_json(), media_type="application/json")


# ---------------------------------------------------------------------------
# Helpers — GET /context
# ---------------------------------------------------------------------------


async def _get_delta_pct(
    db: AsyncSession,
    *,
    now: datetime,
    current_total: Decimal | None,
    target_hours: float,
    tolerance_hours: float,
) -> str | None:
    """Closest history_point total within target_hours ± tolerance_hours ago."""
    if current_total is None:
        return None
    target = now - timedelta(hours=target_hours)
    window_start = target - timedelta(hours=tolerance_hours)
    window_end = target + timedelta(hours=tolerance_hours)
    row = (
        await db.execute(
            sa.text(
                """
                SELECT total_value_usd::text
                FROM history_point
                WHERE total_value_usd IS NOT NULL
                  AND snapshotted_at BETWEEN :window_start AND :window_end
                ORDER BY ABS(EXTRACT(EPOCH FROM (snapshotted_at - :target)))
                LIMIT 1
                """
            ),
            {"window_start": window_start, "window_end": window_end, "target": target},
        )
    ).first()
    if row is None:
        return None
    reference = Decimal(row[0])
    if reference == 0:
        return None
    pct = ((current_total - reference) / reference * 100).quantize(Decimal("0.01"))
    return str(pct)


async def _get_global_headlines(db: AsyncSession, limit: int) -> tuple[int, list[dict]]:
    total = (await db.execute(sa.text("SELECT COUNT(*) FROM asset_news"))).scalar_one()
    rows = await db.execute(
        sa.text(
            """
            SELECT a.symbol, an.title, an.url, an.news_site, an.published_at
            FROM asset_news an
            JOIN asset a ON a.id = an.asset_id
            ORDER BY an.published_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return total, [dict(r) for r in rows.mappings()]


async def _get_global_alerts(db: AsyncSession, limit: int) -> tuple[int, list[dict]]:
    unread_count = (
        await db.execute(sa.text("SELECT COUNT(*) FROM news_alert WHERE read_at IS NULL"))
    ).scalar_one()
    rows = await db.execute(
        sa.text(
            """
            SELECT a.symbol, na.severity, na.summary, na.created_at, na.read_at
            FROM news_alert na
            JOIN asset a ON a.id = na.asset_id
            ORDER BY (na.read_at IS NULL) DESC, na.created_at DESC
            LIMIT :limit
            """
        ),
        {"limit": limit},
    )
    return unread_count, [dict(r) for r in rows.mappings()]


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _build_context_caveats(
    *,
    snapshot_id: str | None,
    total_usd: str | None,
    unpriced_asset_count: int,
    stale_balances: bool,
    stale_prices: bool,
    allocations_shown: int,
    allocations_total: int,
    headlines_shown: int,
    headlines_total: int,
    deltas_all_null: bool,
) -> list[str]:
    caveats: list[str] = []
    if snapshot_id is None:
        caveats.append(
            "No valuation snapshot has been published yet; every portfolio figure is unknown."
        )
    else:
        if total_usd is None:
            caveats.append(
                f"{unpriced_asset_count} asset(s) are unpriced, so total_usd is null, not zero."
            )
        if stale_balances:
            caveats.append(
                "Some balances are carried forward from an earlier scan and may be stale."
            )
        if stale_prices:
            caveats.append("No fresh price quote has been fetched recently; prices may be stale.")
    if allocations_total > allocations_shown:
        caveats.append(
            f"Showing the top {allocations_shown} of {allocations_total} allocations by "
            "value; the rest are omitted to keep this response small."
        )
    if headlines_total > headlines_shown:
        caveats.append(
            f"Showing the {headlines_shown} most recent of {headlines_total} cached headlines."
        )
    if deltas_all_null and total_usd is not None:
        caveats.append(
            "No 24h/7d/30d change could be computed: no historical snapshot was close "
            "enough in time."
        )
    caveats.append(_STALE_PRICE_LAG_NOTE)
    return caveats


# ---------------------------------------------------------------------------
# Helpers — GET /asset/{ref}
# ---------------------------------------------------------------------------


async def _resolve_asset_ref(
    db: AsyncSession, ref: str, *, latest_snapshot_id: str | None
) -> dict | None:
    """Resolve *ref* as a UUID, a contract address, or a symbol (in that order).

    A symbol collision (two assets sharing one ticker, e.g. a manual entry)
    is broken in favour of the asset actually held in the latest published
    snapshot, then the non-excluded one, then the oldest — a best-effort tie
    break, not a promise of uniqueness.
    """
    ref = ref.strip()
    try:
        asset_uuid = uuid.UUID(ref)
    except ValueError:
        asset_uuid = None

    base_select = "SELECT id, token_address, symbol, name, excluded FROM asset"

    if asset_uuid is not None:
        result = await db.execute(sa.text(f"{base_select} WHERE id = :id"), {"id": str(asset_uuid)})
        row = result.mappings().first()
        return dict(row) if row is not None else None

    if ref.lower().startswith("0x") and len(ref) == 42:
        result = await db.execute(
            sa.text(f"{base_select} WHERE token_address = :addr"), {"addr": ref.lower()}
        )
        row = result.mappings().first()
        return dict(row) if row is not None else None

    result = await db.execute(
        sa.text(
            """
            SELECT id, token_address, symbol, name, excluded
            FROM asset a
            WHERE lower(symbol) = lower(:symbol)
            ORDER BY
                EXISTS(
                    SELECT 1 FROM valuation_line vl
                    WHERE vl.asset_id = a.id AND vl.snapshot_id = :snapshot_id
                ) DESC,
                excluded ASC,
                created_at ASC
            LIMIT 1
            """
        ),
        {"symbol": ref, "snapshot_id": latest_snapshot_id},
    )
    row = result.mappings().first()
    return dict(row) if row is not None else None


async def _get_asset_series(db: AsyncSession, asset_id: str, *, now: datetime) -> list[dict]:
    since = now - timedelta(days=_ASSET_SERIES_WINDOW_DAYS)
    rows = await db.execute(
        sa.text(
            """
            SELECT vs.snapshotted_at AS as_of,
                   SUM(vl.value_usd)::text AS value_usd,
                   MAX(vl.price_usd)::text AS price_usd
            FROM valuation_line vl
            JOIN valuation_snapshot vs ON vs.id = vl.snapshot_id
            WHERE vl.asset_id = :asset_id
              AND vs.published_at IS NOT NULL
              AND vs.snapshotted_at >= :since
            GROUP BY vs.id, vs.snapshotted_at
            ORDER BY vs.snapshotted_at ASC
            """
        ),
        {"asset_id": asset_id, "since": since},
    )
    return [dict(r) for r in rows.mappings()]


def _downsample(points: list[dict], cap: int) -> list[dict]:
    if len(points) <= cap:
        return points
    step = -(-len(points) // cap)  # ceil division
    thinned = points[::step]
    if thinned[-1] is not points[-1]:
        thinned.append(points[-1])
    return thinned


async def _get_asset_news(db: AsyncSession, asset_id: str, limit: int) -> tuple[int, list[dict]]:
    total = (
        await db.execute(
            sa.text("SELECT COUNT(*) FROM asset_news WHERE asset_id = :id"), {"id": asset_id}
        )
    ).scalar_one()
    rows = await db.execute(
        sa.text(
            """
            SELECT title, url, news_site, published_at
            FROM asset_news
            WHERE asset_id = :id
            ORDER BY published_at DESC
            LIMIT :limit
            """
        ),
        {"id": asset_id, "limit": limit},
    )
    return total, [dict(r) for r in rows.mappings()]


async def _get_asset_alerts(db: AsyncSession, asset_id: str, limit: int) -> tuple[int, list[dict]]:
    unread_count = (
        await db.execute(
            sa.text("SELECT COUNT(*) FROM news_alert WHERE asset_id = :id AND read_at IS NULL"),
            {"id": asset_id},
        )
    ).scalar_one()
    rows = await db.execute(
        sa.text(
            """
            SELECT severity, summary, created_at, read_at
            FROM news_alert
            WHERE asset_id = :id
            ORDER BY (read_at IS NULL) DESC, created_at DESC
            LIMIT :limit
            """
        ),
        {"id": asset_id, "limit": limit},
    )
    return unread_count, [dict(r) for r in rows.mappings()]


def _build_asset_caveats(
    *,
    allocation: AllocationItemOut | None,
    series_total: int,
    series_shown: int,
    news_total: int,
    news_shown: int,
) -> list[str]:
    caveats: list[str] = []
    if allocation is None:
        caveats.append(
            "This asset is not currently held in any tracked wallet; quantity and value "
            "are unknown, not zero."
        )
    elif allocation.value_usd is None:
        caveats.append("No price is available for this asset; value_usd is null, not zero.")
    if series_total > series_shown:
        caveats.append(
            f"History downsampled to {series_shown} of {series_total} points over the last 30 days."
        )
    if news_total > news_shown:
        caveats.append(
            f"Showing the {news_shown} most recent of {news_total} cached headlines for this asset."
        )
    caveats.append(_STALE_PRICE_LAG_NOTE)
    return caveats


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _na(value: str | None) -> str:
    return value if value is not None else "unknown"


def _context_to_markdown(body: LlmContextResponse) -> str:
    lines = [
        f"# Portfolio context (as of {body.as_of})",
        "",
        f"- total_usd: {_na(body.portfolio.total_usd)}",
        f"- priced_subtotal_usd: {_na(body.portfolio.priced_subtotal_usd)}",
        f"- unpriced_asset_count: {body.portfolio.unpriced_asset_count}",
        f"- quality: incomplete={body.portfolio.incomplete} "
        f"stale_balances={body.portfolio.stale_balances} "
        f"stale_prices={body.portfolio.stale_prices}",
        f"- change: 24h={_na(body.deltas.pct_24h)}% "
        f"7d={_na(body.deltas.pct_7d)}% 30d={_na(body.deltas.pct_30d)}%",
        "",
        "## Top allocations",
    ]
    if body.top_allocations:
        for a in body.top_allocations:
            lines.append(
                f"- {a.symbol}: {_na(a.value_usd)} USD ({_na(a.percentage)}%), "
                f"qty {_na(a.quantity)}, price {_na(a.price_usd)}"
            )
    else:
        lines.append("- (none)")
    lines += ["", "## Headlines"]
    if body.headlines:
        for h in body.headlines:
            lines.append(
                f"- [{h.asset_symbol}] [{h.title}]({h.url}) — {h.news_site}, {h.published_at}"
            )
    else:
        lines.append("- (none)")
    lines += ["", f"## Alerts ({body.alerts.unread_count} unread)"]
    if body.alerts.items:
        for al in body.alerts.items:
            lines.append(f"- [{al.severity}] {al.asset_symbol}: {al.summary} ({al.created_at})")
    else:
        lines.append("- (none)")
    lines += [
        "",
        "## Freshness",
        f"- balances: {_na(body.freshness.balances_as_of)}",
        f"- quotes: {_na(body.freshness.quotes_as_of)}",
        f"- news: {_na(body.freshness.news_as_of)}",
        f"- alerts: {_na(body.freshness.alerts_as_of)}",
        "",
        "## Caveats",
    ]
    lines += [f"- {c}" for c in body.caveats]
    return "\n".join(lines) + "\n"


def _asset_to_markdown(body: LlmAssetResponse) -> str:
    lines = [
        f"# {body.symbol} ({body.name}) — as of {body.as_of}",
        "",
    ]
    if body.contract_address:
        lines.append(f"- contract: {body.contract_address}")
    else:
        lines.append("- contract: native ETH")
    if body.holding is not None:
        lines += [
            f"- quantity: {_na(body.holding.quantity)}",
            f"- value_usd: {_na(body.holding.value_usd)}",
            f"- price_usd: {_na(body.holding.price_usd)}",
            f"- portfolio_share_pct: {_na(body.holding.portfolio_share_pct)}",
            f"- wallet_count: {body.holding.wallet_count}",
        ]
    else:
        lines.append("- not currently held")
    lines += ["", "## 30-day series"]
    if body.series_30d:
        for p in body.series_30d:
            lines.append(f"- {p.as_of}: value={_na(p.value_usd)} price={_na(p.price_usd)}")
    else:
        lines.append("- (no history in the last 30 days)")
    lines += ["", "## News"]
    if body.news:
        for n in body.news:
            lines.append(f"- [{n.title}]({n.url}) — {n.news_site}, {n.published_at}")
    else:
        lines.append("- (none)")
    lines += ["", f"## Alerts ({body.alerts.unread_count} unread)"]
    if body.alerts.items:
        for al in body.alerts.items:
            lines.append(f"- [{al.severity}] {al.summary} ({al.created_at})")
    else:
        lines.append("- (none)")
    lines += ["", "## Caveats"]
    lines += [f"- {c}" for c in body.caveats]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/context", response_model=LlmContextResponse)
async def get_llm_context(
    request: Request,
    auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
) -> Response:
    now = datetime.now(tz=UTC)
    portfolio = await get_portfolio(auth, db)

    sorted_allocations = sorted(
        portfolio.allocations,
        key=lambda a: Decimal(a.value_usd) if a.value_usd is not None else Decimal("-1"),
        reverse=True,
    )
    shown_allocations = sorted_allocations[:_TOP_ALLOCATIONS_LIMIT]

    current_total = Decimal(portfolio.total_usd) if portfolio.total_usd is not None else None
    pct_24h = await _get_delta_pct(
        db, now=now, current_total=current_total, target_hours=24, tolerance_hours=6
    )
    pct_7d = await _get_delta_pct(
        db, now=now, current_total=current_total, target_hours=7 * 24, tolerance_hours=24
    )
    pct_30d = await _get_delta_pct(
        db, now=now, current_total=current_total, target_hours=30 * 24, tolerance_hours=72
    )

    headlines_total, headline_rows = await _get_global_headlines(db, _HEADLINES_LIMIT)
    unread_count, alert_rows = await _get_global_alerts(db, _ALERTS_LIMIT)

    quotes_as_of = (
        await db.execute(sa.text("SELECT MAX(fetched_at) FROM quote_set WHERE status = 'complete'"))
    ).scalar_one()
    news_as_of = (await db.execute(sa.text("SELECT MAX(fetched_at) FROM asset_news"))).scalar_one()
    alerts_as_of = (
        await db.execute(sa.text("SELECT MAX(created_at) FROM news_alert"))
    ).scalar_one()

    body = LlmContextResponse(
        as_of=now.isoformat(),
        currency=portfolio.currency,
        portfolio=PortfolioBlockOut(
            as_of=portfolio.valuation_time,
            total_usd=portfolio.total_usd,
            priced_subtotal_usd=portfolio.priced_subtotal_usd,
            unpriced_asset_count=portfolio.unpriced_asset_count,
            incomplete=portfolio.quality.incomplete,
            stale_balances=portfolio.quality.stale_balances,
            stale_prices=portfolio.quality.stale_prices,
        ),
        top_allocations=[
            TopAllocationOut(
                asset_id=a.asset_id,
                symbol=a.symbol,
                name=a.name,
                value_usd=a.value_usd,
                percentage=a.percentage,
                quantity=a.quantity,
                price_usd=a.price_usd,
            )
            for a in shown_allocations
        ],
        deltas=DeltasOut(pct_24h=pct_24h, pct_7d=pct_7d, pct_30d=pct_30d),
        headlines=[
            HeadlineOut(
                asset_symbol=str(r["symbol"]),
                title=str(r["title"]),
                url=str(r["url"]),
                news_site=str(r["news_site"]),
                published_at=r["published_at"].isoformat(),
            )
            for r in headline_rows
        ],
        alerts=AlertsBlockOut(
            as_of=_isoformat(alerts_as_of),
            unread_count=unread_count,
            items=[
                AlertSummaryOut(
                    asset_symbol=str(r["symbol"]),
                    severity=str(r["severity"]),
                    summary=str(r["summary"]),
                    created_at=r["created_at"].isoformat(),
                    read_at=r["read_at"].isoformat() if r["read_at"] is not None else None,
                )
                for r in alert_rows
            ],
        ),
        freshness=FreshnessOut(
            balances_as_of=portfolio.balance_observed_at,
            quotes_as_of=_isoformat(quotes_as_of),
            news_as_of=_isoformat(news_as_of),
            alerts_as_of=_isoformat(alerts_as_of),
        ),
        caveats=_build_context_caveats(
            snapshot_id=portfolio.snapshot_id,
            total_usd=portfolio.total_usd,
            unpriced_asset_count=portfolio.unpriced_asset_count,
            stale_balances=portfolio.quality.stale_balances,
            stale_prices=portfolio.quality.stale_prices,
            allocations_shown=len(shown_allocations),
            allocations_total=len(sorted_allocations),
            headlines_shown=len(headline_rows),
            headlines_total=headlines_total,
            deltas_all_null=pct_24h is None and pct_7d is None and pct_30d is None,
        ),
        request_id=str(uuid.uuid4()),
    )
    return _render(body, _context_to_markdown(body), request)


@router.get("/asset/{ref}", response_model=LlmAssetResponse)
async def get_llm_asset(
    ref: str,
    request: Request,
    auth: Annotated[ReadPrincipal, Depends(_require_read_auth)],
    db: AsyncSession = Depends(get_db),
) -> Response:
    now = datetime.now(tz=UTC)
    portfolio = await get_portfolio(auth, db)

    asset_row = await _resolve_asset_ref(db, ref, latest_snapshot_id=portfolio.snapshot_id)
    if asset_row is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="asset not found")

    asset_id = str(asset_row["id"])
    token_address = str(asset_row["token_address"])
    is_native = is_native_eth(token_address)

    allocation = next((a for a in portfolio.allocations if a.asset_id == asset_id), None)
    holding: AssetHoldingOut | None = None
    if allocation is not None:
        holding = AssetHoldingOut(
            quantity=allocation.quantity,
            value_usd=allocation.value_usd,
            price_usd=allocation.price_usd,
            portfolio_share_pct=allocation.percentage if allocation.value_usd is not None else None,
            wallet_count=allocation.wallet_count,
            included=allocation.included,
        )

    series_rows = await _get_asset_series(db, asset_id, now=now)
    shown_series = _downsample(series_rows, _ASSET_SERIES_CAP)

    news_total, news_rows = await _get_asset_news(db, asset_id, _ASSET_NEWS_LIMIT)
    asset_unread_count, alert_rows = await _get_asset_alerts(db, asset_id, _ASSET_ALERTS_LIMIT)

    body = LlmAssetResponse(
        as_of=now.isoformat(),
        asset_id=asset_id,
        symbol=str(asset_row["symbol"]),
        name=str(asset_row["name"]),
        contract_address=None if is_native else token_address,
        is_native=is_native,
        holding=holding,
        series_30d=[
            AssetSeriesPointOut(
                as_of=r["as_of"].isoformat(),
                value_usd=r["value_usd"],
                price_usd=r["price_usd"],
            )
            for r in shown_series
        ],
        news=[
            AssetNewsItemOut(
                title=str(r["title"]),
                url=str(r["url"]),
                news_site=str(r["news_site"]),
                published_at=r["published_at"].isoformat(),
            )
            for r in news_rows
        ],
        alerts=AssetAlertsBlockOut(
            unread_count=asset_unread_count,
            items=[
                AssetAlertItemOut(
                    severity=str(r["severity"]),
                    summary=str(r["summary"]),
                    created_at=r["created_at"].isoformat(),
                    read_at=r["read_at"].isoformat() if r["read_at"] is not None else None,
                )
                for r in alert_rows
            ],
        ),
        caveats=_build_asset_caveats(
            allocation=allocation,
            series_total=len(series_rows),
            series_shown=len(shown_series),
            news_total=news_total,
            news_shown=len(news_rows),
        ),
        request_id=str(uuid.uuid4()),
    )
    return _render(body, _asset_to_markdown(body), request)
