"""Quote-refresh job handler — fetches prices for currently held assets (T054 / US2 / AUD-67).

Only assets with at least one non-zero balance observation are priced
(held-asset-only).  Assets with no recent observation, zero balance, or
that are excluded are skipped.

The job inserts a quote_set row, populates quote_observation rows for each
successfully priced asset, and marks the set complete.  On any provider
error the set is marked failed but the job itself does not raise so the
worker can continue to the next run.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.providers.coingecko_demo import CoinGeckoError, CoinGeckoProvider
from audr.settings.quotes import get_coingecko_api_key

logger = logging.getLogger(__name__)

_ETH_NATIVE_ADDRESS = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


async def handle_quote_refresh(session: AsyncSession, run_id: uuid.UUID) -> None:
    """Fetch and store price quotes for all currently held assets."""
    api_key = await get_coingecko_api_key(session)
    if not api_key:
        logger.warning(
            "quote_refresh skipped — no CoinGecko API key configured run_id=%s", run_id
        )
        return

    token_addresses = await _get_held_asset_addresses(session)
    if not token_addresses:
        logger.info("quote_refresh skipped — no held assets run_id=%s", run_id)
        return

    quote_set_id = await _insert_quote_set(session, provider="coingecko")
    await session.flush()

    try:
        async with CoinGeckoProvider(api_key=api_key) as provider:
            prices = await provider.get_prices(token_addresses)
    except CoinGeckoError as exc:
        logger.warning(
            "quote_refresh provider error run_id=%s quote_set=%s: %s",
            run_id,
            quote_set_id,
            exc,
        )
        await _mark_quote_set(session, quote_set_id, "failed")
        await session.flush()
        return

    asset_id_map = await _get_asset_id_map(session, token_addresses)

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

    # Resurrection fence: if the run was cancelled (e.g. by a concurrent purge)
    # or the integration was deleted while we were fetching, discard results.
    run_check = await session.execute(
        sa.text("SELECT status FROM job_run WHERE id = :id"),
        {"id": str(run_id)},
    )
    run_row = run_check.first()
    if run_row is None or run_row[0] != "in_progress":
        logger.warning(
            "quote_refresh: run %s is no longer in_progress — discarding results",
            run_id,
        )
        return

    int_check = await session.execute(
        sa.text("SELECT 1 FROM integration WHERE kind = 'coingecko' LIMIT 1"),
    )
    if int_check.first() is None:
        logger.warning(
            "quote_refresh: coingecko integration deleted during run — discarding results run_id=%s",
            run_id,
        )
        return

    await _mark_quote_set(session, quote_set_id, "complete")
    await session.flush()

    logger.info(
        "quote_refresh run_id=%s quote_set=%s priced=%d/%d",
        run_id,
        quote_set_id,
        len(prices),
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
