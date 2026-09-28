"""Password-protected provider-data purge with preview, job fencing, and chain-record preservation (T086 / US4)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from sqlalchemy.ext.asyncio import AsyncSession

from audr.auth.service import AuthenticationError

# Same Argon2id parameters as auth.service (OWASP minimum config).
_hasher = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)


async def preview_purge(session: AsyncSession, *, kind: str) -> dict:  # type: ignore[type-arg]
    """Return counts of what WOULD be deleted, without deleting anything.

    Returns::

        {
            "kind": str,
            "quote_observation_count": int,
            "quote_set_count": int,
            "valuation_line_count": int,
            "integration_count": int,
        }
    """
    if kind == "coingecko":
        result = await session.execute(
            sa.text(
                "SELECT COUNT(*) FROM quote_observation qo"
                " JOIN quote_set qs ON qs.id = qo.quote_set_id"
                " WHERE qs.provider = 'coingecko'"
            )
        )
        quote_obs_count: int = int(result.scalar() or 0)

        result = await session.execute(
            sa.text("SELECT COUNT(*) FROM quote_set WHERE provider = 'coingecko'")
        )
        quote_set_count: int = int(result.scalar() or 0)

        # Only count valuation_line rows that carry coingecko-derived prices.
        result = await session.execute(
            sa.text("SELECT COUNT(*) FROM valuation_line WHERE price_usd IS NOT NULL")
        )
        valuation_line_count: int = int(result.scalar() or 0)
    else:
        # RPC is not a quote provider — no quote or valuation monetary data to purge.
        quote_obs_count = 0
        quote_set_count = 0
        valuation_line_count = 0

    result = await session.execute(
        sa.text("SELECT COUNT(*) FROM integration WHERE kind = :kind"),
        {"kind": kind},
    )
    integration_count: int = int(result.scalar() or 0)

    return {
        "kind": kind,
        "quote_observation_count": quote_obs_count,
        "quote_set_count": quote_set_count,
        "valuation_line_count": valuation_line_count,
        "integration_count": integration_count,
    }


async def execute_purge(
    session: AsyncSession, *, kind: str, password: str
) -> dict:  # type: ignore[type-arg]
    """Purge provider-specific monetary data, preserving all on-chain records.

    1. Verifies the caller's password against the stored owner hash.
    2. Performs a preview to collect deletion counts.
    3. Deletes provider rows (monetary/provider data only).
    4. Logs the operation in provider_purge_log.

    NEVER deletes balance_observation, wallet, or asset rows.

    Raises AuthenticationError if the password is wrong.
    """
    # --- Step 1: Authenticate ---
    result = await session.execute(sa.text("SELECT argon2_hash FROM owner LIMIT 1"))
    row = result.first()
    if row is None:
        raise AuthenticationError("no owner configured")

    try:
        _hasher.verify(row[0], password)
    except VerifyMismatchError:
        raise AuthenticationError("incorrect password") from None

    # --- Step 2: Preview (capture counts before deletion) ---
    preview = await preview_purge(session, kind=kind)

    # --- Step 3: Delete provider data ---
    if kind == "coingecko":
        # Job fencing: cancel any pending/in_progress quote_refresh jobs.
        # claim_job additionally checks integration existence, so no new
        # claims will succeed once the integration row is deleted below.
        await session.execute(
            sa.text(
                "UPDATE job_run SET status = 'cancelled'"
                " WHERE kind = 'quote_refresh'"
                " AND status IN ('pending', 'in_progress')"
            )
        )

        # Delete quote_observation rows linked to coingecko quote_sets.
        await session.execute(
            sa.text(
                "DELETE FROM quote_observation"
                " WHERE quote_set_id IN ("
                "   SELECT id FROM quote_set WHERE provider = 'coingecko'"
                " )"
            )
        )

        # Delete coingecko quote_set rows.
        await session.execute(
            sa.text("DELETE FROM quote_set WHERE provider = 'coingecko'")
        )

        # Delete history_point rows for snapshots that will become empty after
        # we remove the priced valuation_lines (those with no unpriced sibling).
        await session.execute(
            sa.text(
                "DELETE FROM history_point"
                " WHERE snapshot_id IN ("
                "   SELECT id FROM valuation_snapshot"
                "   WHERE id NOT IN ("
                "     SELECT DISTINCT snapshot_id FROM valuation_line"
                "     WHERE price_usd IS NULL"
                "   )"
                " )"
            )
        )

        # Delete valuation_line rows that carry coingecko-derived prices.
        await session.execute(
            sa.text("DELETE FROM valuation_line WHERE price_usd IS NOT NULL")
        )

        # Delete valuation_snapshot rows that are now childless.
        await session.execute(
            sa.text(
                "DELETE FROM valuation_snapshot"
                " WHERE id NOT IN (SELECT DISTINCT snapshot_id FROM valuation_line)"
            )
        )

        # Delete the coingecko integration row.
        await session.execute(
            sa.text("DELETE FROM integration WHERE kind = 'coingecko'")
        )

    elif kind == "rpc":
        # Job fencing: cancel pending/in_progress balance_scan and discovery jobs.
        # claim_job checks integration existence, so no new claims succeed once
        # the rpc integration row is deleted below.
        await session.execute(
            sa.text(
                "UPDATE job_run SET status = 'cancelled'"
                " WHERE kind IN ('balance_scan', 'discovery')"
                " AND status IN ('pending', 'in_progress')"
            )
        )

        # Delete the rpc integration row.
        await session.execute(
            sa.text("DELETE FROM integration WHERE kind = 'rpc'")
        )

    # NEVER delete balance_observation, wallet, or asset rows.

    # --- Step 6: Log the purge operation ---
    log_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO provider_purge_log"
            " (id, kind, purged_by, quote_observations_deleted, valuation_lines_deleted)"
            " VALUES (:id, :kind, 'owner', :qobs, :vlines)"
        ),
        {
            "id": log_id,
            "kind": kind,
            "qobs": preview["quote_observation_count"],
            "vlines": preview["valuation_line_count"],
        },
    )

    await session.flush()

    return {
        "kind": kind,
        "purged": True,
        "quote_observations_deleted": preview["quote_observation_count"],
        "valuation_lines_deleted": preview["valuation_line_count"],
    }
