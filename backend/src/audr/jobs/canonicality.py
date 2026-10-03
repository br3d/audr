"""Canonicality rechecks: detect reorg-affected history points and schedule re-scans (T068 / US3 / AUD-81).

invalidate_observation():
  Insert a balance_observation_invalidation record for an observation affected by
  a reorg or verification failure.  After recording the invalidation, mark all
  history_point rows whose constituent valuation_lines used that observation as
  non-canonical.

recheck_canonicality():
  Sweep all history_points currently marked is_canonical=true and check whether
  any of their constituent observations are now in balance_observation_invalidation.
  This is a catch-up pass for observations invalidated after their history_points
  were published.

schedule_replacement_scans():
  For each non-canonical history_point, find the (wallet_id, asset_id) pairs whose
  observations were invalidated and return them for the balance-scan job to re-read.

Rules:
- Invalidation records are append-only; existing observations are never modified.
- history_point.is_canonical is the only mutable field changed by this module.
- Duplicate invalidation calls for the same observation_id are silently ignored
  (the UNIQUE constraint on balance_observation_invalidation handles this).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


@dataclass
class ScanTarget:
    wallet_id: uuid.UUID
    asset_id: uuid.UUID
    token_address: str


async def invalidate_observation(
    session: AsyncSession,
    *,
    observation_id: uuid.UUID,
    reason: str,
    reorg_depth: int | None = None,
) -> bool:
    """Record that an observation is no longer canonical.

    Returns True if a new record was inserted, False if already invalidated.
    Marks all history_points that reference this observation as non-canonical.
    """
    if reason not in ("reorg", "verification_pending"):
        raise ValueError(f"invalid invalidation reason: {reason!r}")

    try:
        await session.execute(
            sa.text(
                """
                INSERT INTO balance_observation_invalidation
                  (id, observation_id, reason, invalidated_at, reorg_depth)
                VALUES (:id, :obs, :reason, :now, :depth)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "obs": str(observation_id),
                "reason": reason,
                "now": datetime.now(tz=UTC),
                "depth": reorg_depth,
            },
        )
    except Exception as exc:
        # Unique constraint violation: already invalidated.
        if "uq_balance_observation_invalidation_observation_id" in str(exc):
            logger.debug("observation %s already invalidated — skipping", observation_id)
            return False
        raise

    affected = await _mark_affected_history_points(session, observation_id)
    logger.info(
        "observation %s invalidated reason=%s affected_history_points=%d",
        observation_id,
        reason,
        affected,
    )
    return True


async def recheck_canonicality(session: AsyncSession) -> int:
    """Sweep canonical history_points for newly-invalidated constituent observations.

    Returns the count of history_points flipped to non-canonical.
    """
    result = await session.execute(
        sa.text(
            """
            UPDATE history_point hp
            SET is_canonical = false
            WHERE hp.is_canonical = true
              AND EXISTS (
                  SELECT 1
                  FROM valuation_line vl
                  JOIN balance_observation_invalidation boi
                    ON boi.observation_id = vl.observation_id
                  WHERE vl.snapshot_id = hp.snapshot_id
              )
            RETURNING hp.id
            """
        )
    )
    rows = result.fetchall()
    count = len(rows)
    if count:
        logger.warning("canonicality recheck: %d history_point(s) flipped to non-canonical", count)
    return count


async def schedule_replacement_scans(
    session: AsyncSession,
) -> list[ScanTarget]:
    """Return (wallet_id, asset_id, token_address) tuples needing re-scan.

    These are the wallet/asset pairs whose observations were invalidated and
    that appear in at least one non-canonical history_point.
    """
    result = await session.execute(
        sa.text(
            """
            SELECT DISTINCT
                vl.wallet_id::text,
                vl.asset_id::text,
                a.token_address
            FROM history_point hp
            JOIN valuation_line vl ON vl.snapshot_id = hp.snapshot_id
            JOIN balance_observation_invalidation boi
                ON boi.observation_id = vl.observation_id
            JOIN asset a ON a.id = vl.asset_id
            WHERE hp.is_canonical = false
            """
        )
    )
    targets = [
        ScanTarget(
            wallet_id=uuid.UUID(row[0]),
            asset_id=uuid.UUID(row[1]),
            token_address=row[2],
        )
        for row in result
    ]
    if targets:
        logger.info(
            "scheduled %d replacement scan(s) for non-canonical history_points",
            len(targets),
        )
    return targets


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _mark_affected_history_points(
    session: AsyncSession,
    observation_id: uuid.UUID,
) -> int:
    result = await session.execute(
        sa.text(
            """
            UPDATE history_point hp
            SET is_canonical = false
            WHERE hp.is_canonical = true
              AND EXISTS (
                  SELECT 1 FROM valuation_line vl
                  WHERE vl.snapshot_id = hp.snapshot_id
                    AND vl.observation_id = :obs
              )
            RETURNING hp.id
            """
        ),
        {"obs": str(observation_id)},
    )
    return len(result.fetchall())
