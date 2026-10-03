"""Quote-refresh job handler — fetches prices for currently held assets (T054 / US2 / AUD-67).

Only assets with at least one non-zero balance observation are priced
(held-asset-only).  Assets with no recent observation, zero balance, or
that are excluded are skipped.

Provider selection (AUD-358): CoinGecko Demo requires an API key that a
fresh install never has, so it used to be the only provider and
quote_refresh was a permanent no-op out of the box. CoinMarketCap's public
`/public-api/v1/*` endpoints work without a key, so that's now the default;
CoinGecko is used instead whenever the owner has saved a key for it, since
its address-based `/simple/token_price` endpoint covers more tokens.

The job inserts a quote_set row, populates quote_observation rows for each
successfully priced asset, and marks the set complete.  On any provider
error the set is marked failed and the job re-raises (AUD-318) so the worker
records the run itself as failed — a swallowed error here would report a
provider outage as a successful run.

Per-asset freshness (AUD-370): a held asset whose most recent price is still
younger than the quote_refresh schedule's `freshness_s` is skipped on the
provider call and its last known price is carried forward into the new
quote_set instead — this is what keeps a run that's triggered sooner than a
full schedule interval (a reclaimed stale lease, a future manual trigger)
from re-asking the provider about assets nothing has changed for, which is
exactly the kind of extra request volume that trips CoinMarketCap's
anonymous rate limit.
"""

from __future__ import annotations

import functools
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.cmc_catalog import resolve_cmc_ids
from audr.assets.constants import NATIVE_ETH_ADDRESS
from audr.jobs.policy import get_shared_cmc_rate_limiter
from audr.jobs.store import JobKind, enqueue_job
from audr.operations.status import ComponentStatus, QuoteStatus
from audr.providers.coingecko_demo import CoinGeckoError, CoinGeckoProvider
from audr.providers.coinmarketcap_public import CoinMarketCapError, CoinMarketCapProvider
from audr.settings.quotes import get_coingecko_api_key
from audr.settings.schedules import get_schedule

logger = logging.getLogger(__name__)

_PROVIDER_ERRORS = (CoinGeckoError, CoinMarketCapError)

# Fallback per-asset freshness when the quote_refresh schedule row is missing
# or has no freshness_s set — matches the in-code default in
# audr.jobs.store._DEFAULT_FRESHNESS_S["quote_refresh"] (AUD-366).
_DEFAULT_QUOTE_FRESHNESS_S = 3600

# A price older than this multiple of freshness_s is "stale" for /health/ready
# purposes even though it isn't NULL — the job may be failing silently on
# retries while an earlier price keeps the portfolio looking fine (AUD-370).
_STALE_PRICE_FRESHNESS_MULTIPLE = 2


async def get_active_quote_provider(session: AsyncSession) -> str:
    """Return the provider quote_refresh will use: 'coingecko' or 'coinmarketcap'.

    CoinMarketCap's keyless endpoint is always available, so this never
    returns "none" — CoinGecko is only used once the owner opts in with a key.
    """
    api_key = await get_coingecko_api_key(session)
    return "coingecko" if api_key else "coinmarketcap"


async def get_quote_status(session: AsyncSession) -> QuoteStatus:
    """Report the active quote provider and how many held assets lack a price.

    Reads the most recent valuation snapshot rather than re-deriving
    "held assets" here, so this always matches what /holdings would show.
    No snapshot yet (fresh install, worker hasn't run valuation once) reports
    zero holdings and zero unpriced — not a false DEGRADED before the first
    run has had a chance to complete.

    Also reports DEGRADED when held assets are priced but the last
    *successful* quote_set is older than 2x freshness_s (AUD-370) — a
    portfolio that still shows prices from hours ago because quote_refresh
    keeps failing (e.g. a sustained CoinMarketCap 429) must not look healthy
    just because every price happens to be non-NULL.
    """
    provider = await get_active_quote_provider(session)
    result = await session.execute(
        sa.text(
            """
            SELECT
                COUNT(*) FILTER (WHERE vl.price_usd IS NULL) AS unpriced,
                COUNT(*) AS total
            FROM valuation_line vl
            WHERE vl.snapshot_id = (
                SELECT id FROM valuation_snapshot
                ORDER BY snapshotted_at DESC LIMIT 1
            )
            """
        )
    )
    row = result.first()
    unpriced_count = int(row[0]) if row and row[0] is not None else 0
    total_count = int(row[1]) if row and row[1] is not None else 0

    stale = False
    if total_count > 0 and unpriced_count < total_count:
        freshness_s = await _get_quote_freshness_s(session)
        latest = await session.execute(
            sa.text("SELECT MAX(fetched_at) FROM quote_set WHERE status = 'complete'")
        )
        fetched_at = latest.scalar()
        if fetched_at is None:
            stale = True
        else:
            age_s = (datetime.now(UTC) - fetched_at).total_seconds()
            stale = age_s > _STALE_PRICE_FRESHNESS_MULTIPLE * freshness_s

    status = (
        ComponentStatus.DEGRADED
        if (total_count > 0 and unpriced_count == total_count) or stale
        else ComponentStatus.OK
    )
    return QuoteStatus(provider=provider, unpriced_count=unpriced_count, status=status)


async def _get_quote_freshness_s(session: AsyncSession) -> int:
    """Return the configured freshness_s for quote_refresh, or the in-code default."""
    schedule = await get_schedule(session, kind="quote_refresh")
    if schedule is not None and schedule["freshness_s"] is not None:
        return int(schedule["freshness_s"])
    return _DEFAULT_QUOTE_FRESHNESS_S


async def handle_quote_refresh(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Fetch and store price quotes for all currently held assets.

    Only assets whose latest known price is older than freshness_s are
    actually sent to the provider (AUD-370); assets priced more recently than
    that have their last price carried forward into the new quote_set
    unchanged. If nothing is stale, the provider is never called this run.
    """
    token_addresses = await _get_held_asset_addresses(session)
    if not token_addresses:
        logger.info("quote_refresh skipped — no held assets run_id=%s", run_id)
        return

    provider_name = await get_active_quote_provider(session)
    freshness_s = await _get_quote_freshness_s(session)
    cached_prices = await _get_cached_prices(session, token_addresses, freshness_s=freshness_s)
    stale_addresses = [a for a in token_addresses if a not in cached_prices]

    quote_set_id = await _insert_quote_set(session, provider=provider_name)
    await session.flush()

    fetched_prices: dict[str, Decimal] = {}
    if stale_addresses:
        try:
            if provider_name == "coingecko":
                api_key = await get_coingecko_api_key(session)
                assert api_key is not None  # get_active_quote_provider already checked
                async with CoinGeckoProvider(api_key=api_key) as provider:
                    fetched_prices = await provider.get_prices(stale_addresses)
            else:
                resolver = functools.partial(resolve_cmc_ids, session)
                async with CoinMarketCapProvider(
                    resolver=resolver, rate_limiter=get_shared_cmc_rate_limiter()
                ) as cmc_provider:
                    fetched_prices = await cmc_provider.get_prices(stale_addresses)
        except _PROVIDER_ERRORS as exc:
            logger.warning(
                "quote_refresh provider error run_id=%s quote_set=%s provider=%s: %s",
                run_id,
                quote_set_id,
                provider_name,
                exc,
            )
            await _mark_quote_set(session, quote_set_id, "failed")
            await session.commit()
            # Re-raise so the worker records this run as failed instead of
            # completed (AUD-318) — a silently "successful" run would advance
            # schedule.last_run_at and mask the provider outage from retry budget
            # and history.
            raise

    prices = {**cached_prices, **fetched_prices}
    logger.info(
        "quote_refresh run_id=%s cached=%d stale=%d",
        run_id,
        len(cached_prices),
        len(stale_addresses),
    )

    # Fencing: re-check run status after the external API call — the purge may
    # have cancelled the run while we were waiting for the provider response.
    # The coingecko integration itself is only fenced for the coingecko
    # provider: CoinMarketCap is keyless and has no integration row to purge,
    # so there's nothing to re-check for it beyond run liveness.
    run_active = await session.execute(
        sa.text("SELECT 1 FROM job_run WHERE id = :id AND status = 'in_progress'"),
        {"id": str(run_id)},
    )
    fenced = run_active.first() is None
    if not fenced and provider_name == "coingecko":
        integration_alive = await session.execute(
            sa.text("SELECT 1 FROM integration WHERE kind = 'coingecko' LIMIT 1"),
        )
        fenced = integration_alive.first() is None
    if fenced:
        logger.info(
            "quote_refresh fenced: run cancelled or integration removed run_id=%s",
            run_id,
        )
        await session.execute(
            sa.text("DELETE FROM quote_set WHERE id = :id"),
            {"id": str(quote_set_id)},
        )
        await session.flush()
        return

    asset_id_map = await _get_asset_id_map(session, token_addresses)

    obs_count = 0
    resolved_addresses: set[str] = set()
    for address, price_usd in prices.items():
        asset_id = asset_id_map.get(address.lower())
        if asset_id is None:
            continue
        await session.execute(
            sa.text(
                """
                INSERT INTO quote_observation (id, quote_set_id, asset_id, price_usd)
                VALUES (:id, :qset, :asset, :price)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "qset": str(quote_set_id),
                "asset": str(asset_id),
                "price": str(price_usd),
            },
        )
        obs_count += 1
        resolved_addresses.add(address.lower())

    await _update_price_availability(session, token_addresses, resolved_addresses)

    # Mark 'empty' (not 'complete') when no observations were inserted so that
    # _get_latest_prices never shadows an earlier set that carried real prices.
    final_status = "complete" if obs_count > 0 else "empty"
    await _mark_quote_set(session, quote_set_id, final_status)
    await enqueue_job(session, kind=JobKind.VALUATION)
    await session.flush()

    logger.info(
        "quote_refresh run_id=%s quote_set=%s status=%s priced=%d/%d",
        run_id,
        quote_set_id,
        final_status,
        obs_count,
        len(token_addresses),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _get_held_asset_addresses(session: AsyncSession) -> list[str]:
    """Return lowercase token addresses that have at least one non-zero balance."""
    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT a.token_address
            FROM balance_observation bo
            JOIN asset a ON a.id = bo.asset_id
            WHERE bo.raw_amount > 0
              AND NOT COALESCE(a.excluded, false)
            """
        )
    )
    return [row[0] for row in result]


async def _get_cached_prices(
    session: AsyncSession, token_addresses: list[str], *, freshness_s: int
) -> dict[str, Decimal]:
    """Return {address: price_usd} for held assets priced within freshness_s.

    Looks at each asset's single most recent observation from a *complete*
    quote_set, regardless of which quote_set that came from — not just the
    latest one — so a price fetched two runs ago still counts as fresh if it's
    within the window. An asset with no complete observation yet (new, or
    previously unresolved "dust") is never cached and is always requested.
    """
    if freshness_s <= 0:
        return {}
    result = await session.execute(
        sa.text(
            """
            SELECT a.token_address, latest.price_usd::text
            FROM asset a
            JOIN LATERAL (
                SELECT qo.price_usd, qs.fetched_at
                FROM quote_observation qo
                JOIN quote_set qs ON qs.id = qo.quote_set_id
                WHERE qo.asset_id = a.id AND qs.status = 'complete'
                ORDER BY qs.fetched_at DESC
                LIMIT 1
            ) latest ON true
            WHERE a.token_address = ANY(:addrs)
              AND latest.fetched_at >= now() - make_interval(secs => :freshness_s)
            """
        ),
        {"addrs": token_addresses, "freshness_s": freshness_s},
    )
    return {row[0]: Decimal(row[1]) for row in result}


async def _insert_quote_set(session: AsyncSession, *, provider: str) -> uuid.UUID:
    qset_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            "INSERT INTO quote_set (id, provider, fetched_at, status, created_at)"
            " VALUES (:id, :provider, :now, 'pending', :now)"
        ),
        {"id": str(qset_id), "provider": provider, "now": now},
    )
    return qset_id


async def _mark_quote_set(session: AsyncSession, quote_set_id: uuid.UUID, status: str) -> None:
    await session.execute(
        sa.text("UPDATE quote_set SET status = :status WHERE id = :id"),
        {"status": status, "id": str(quote_set_id)},
    )


async def _get_asset_id_map(
    session: AsyncSession, token_addresses: list[str]
) -> dict[str, uuid.UUID]:
    """Return {lowercase_token_address: asset_id} for the given addresses."""
    result = await session.execute(
        sa.text("SELECT id, token_address FROM asset WHERE token_address = ANY(:addrs)"),
        {"addrs": token_addresses},
    )
    return {row[1]: uuid.UUID(str(row[0])) for row in result}


async def _update_price_availability(
    session: AsyncSession, requested: list[str], resolved: set[str]
) -> None:
    """Record which requested assets the provider confirmed it cannot price (AUD-361).

    An address the provider didn't return a price for gets
    ``price_unavailable_since`` set — this is the signal that distinguishes a
    holding the provider genuinely doesn't know (never blocks total_usd) from
    one that simply hasn't been asked about yet (does). An address that
    resolves again after previously missing has the flag cleared.
    """
    now = datetime.now(tz=UTC)
    unresolved = [addr for addr in requested if addr.lower() not in resolved]
    if resolved:
        await session.execute(
            sa.text(
                "UPDATE asset SET price_unavailable_since = NULL"
                " WHERE token_address = ANY(:addrs) AND price_unavailable_since IS NOT NULL"
            ),
            {"addrs": list(resolved)},
        )
    if unresolved:
        await session.execute(
            sa.text(
                "UPDATE asset SET price_unavailable_since = :now WHERE token_address = ANY(:addrs)"
            ),
            {"addrs": unresolved, "now": now},
        )
