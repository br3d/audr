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
"""

from __future__ import annotations

import functools
import logging
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.assets.cmc_catalog import resolve_cmc_ids
from audr.assets.constants import NATIVE_ETH_ADDRESS
from audr.jobs.store import JobKind, enqueue_job
from audr.operations.status import ComponentStatus, QuoteStatus
from audr.providers.coingecko_demo import CoinGeckoError, CoinGeckoProvider
from audr.providers.coinmarketcap_public import CoinMarketCapError, CoinMarketCapProvider
from audr.settings.quotes import get_coingecko_api_key

logger = logging.getLogger(__name__)

_PROVIDER_ERRORS = (CoinGeckoError, CoinMarketCapError)


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

    status = (
        ComponentStatus.DEGRADED
        if total_count > 0 and unpriced_count == total_count
        else ComponentStatus.OK
    )
    return QuoteStatus(provider=provider, unpriced_count=unpriced_count, status=status)


async def handle_quote_refresh(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Fetch and store price quotes for all currently held assets."""
    token_addresses = await _get_held_asset_addresses(session)
    if not token_addresses:
        logger.info("quote_refresh skipped — no held assets run_id=%s", run_id)
        return

    provider_name = await get_active_quote_provider(session)
    quote_set_id = await _insert_quote_set(session, provider=provider_name)
    await session.flush()

    try:
        if provider_name == "coingecko":
            api_key = await get_coingecko_api_key(session)
            assert api_key is not None  # get_active_quote_provider already checked
            async with CoinGeckoProvider(api_key=api_key) as provider:
                prices = await provider.get_prices(token_addresses)
        else:
            resolver = functools.partial(resolve_cmc_ids, session)
            async with CoinMarketCapProvider(resolver=resolver) as cmc_provider:
                prices = await cmc_provider.get_prices(token_addresses)
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


async def _insert_quote_set(
    session: AsyncSession, *, provider: str
) -> uuid.UUID:
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


async def _mark_quote_set(
    session: AsyncSession, quote_set_id: uuid.UUID, status: str
) -> None:
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
