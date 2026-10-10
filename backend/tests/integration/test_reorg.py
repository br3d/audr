"""Integration tests for reorg handling and canonicality (T065 / US3 / AUD-78).

Covers:
- Observation invalidation records are inserted correctly
- Duplicate invalidation calls are idempotent (UNIQUE constraint respected)
- Non-canonical history_points are identified when their observations are invalidated
- recheck_canonicality() sweeps and flips affected points
- schedule_replacement_scans() returns the correct wallet/asset pairs
- Verification-pending invalidation reason
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.canonicality import (
    invalidate_observation,
    recheck_canonicality,
    schedule_replacement_scans,
)
from audr.portfolio.history import materialize_history_point

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Helpers (shared with test_history.py but duplicated to keep files independent)
# ---------------------------------------------------------------------------


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wid = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address, label_ciphertext, status) VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wid), "addr": address.lower()},
    )
    return wid


async def _insert_asset(
    session: AsyncSession,
    *,
    token_address: str,
    symbol: str = "TKN",
    decimals: int = 18,
) -> uuid.UUID:
    aid = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO asset (id, token_address, symbol, name, decimals, source, excluded)
            VALUES (:id, :addr, :sym, :name, :dec, 'manual', false)
            """
        ),
        {
            "id": str(aid),
            "addr": token_address.lower(),
            "sym": symbol,
            "name": symbol,
            "dec": decimals,
        },
    )
    return aid


async def _insert_balance_observation(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    raw_amount: int = 1_000_000_000_000_000_000,
    block_number: int = 12345678,
) -> uuid.UUID:
    obs_id = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO balance_observation
              (id, wallet_id, asset_id, raw_amount, block_number, observed_at)
            VALUES (:id, :wid, :aid, :raw, :block, now())
            """
        ),
        {
            "id": str(obs_id),
            "wid": str(wallet_id),
            "aid": str(asset_id),
            "raw": raw_amount,
            "block": block_number,
        },
    )
    return obs_id


async def _insert_snapshot(session: AsyncSession, *, quality: str = "complete") -> uuid.UUID:
    sid = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await session.execute(
        sa.text(
            """
            INSERT INTO valuation_snapshot (id, snapshotted_at, quality, published_at, input_key)
            VALUES (:id, :ts, :quality, :ts, :input_key)
            """
        ),
        {"id": str(sid), "ts": now, "quality": quality, "input_key": str(sid)},
    )
    return sid


async def _insert_valuation_line(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
    wallet_id: uuid.UUID,
    asset_id: uuid.UUID,
    observation_id: uuid.UUID | None = None,
    value_usd: str | None = "1.0",
) -> uuid.UUID:
    lid = uuid.uuid4()
    await session.execute(
        sa.text(
            """
            INSERT INTO valuation_line
              (id, snapshot_id, wallet_id, asset_id, raw_amount, block_number,
               price_usd, value_usd, observation_id)
            VALUES (:id, :sid, :wid, :aid, 1, 12345678, :price, :value, :obs)
            """
        ),
        {
            "id": str(lid),
            "sid": str(snapshot_id),
            "wid": str(wallet_id),
            "aid": str(asset_id),
            "price": "1.0" if value_usd else None,
            "value": value_usd,
            "obs": str(observation_id) if observation_id else None,
        },
    )
    return lid


# ---------------------------------------------------------------------------
# Tests: observation invalidation
# ---------------------------------------------------------------------------


async def test_invalidate_observation_inserts_record(
    db_session: AsyncSession,
) -> None:
    wallet_id = await _insert_wallet(db_session, "0xa001000000000000000000000000000000000001")
    asset_id = await _insert_asset(
        db_session, token_address="0xa002000000000000000000000000000000000002"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    result = await invalidate_observation(
        db_session, observation_id=obs_id, reason="reorg", reorg_depth=3
    )
    assert result is True

    row = (
        await db_session.execute(
            sa.text(
                "SELECT reason, reorg_depth FROM balance_observation_invalidation"
                " WHERE observation_id = :obs"
            ),
            {"obs": str(obs_id)},
        )
    ).first()
    assert row is not None
    assert row[0] == "reorg"
    assert row[1] == 3


async def test_invalidate_observation_idempotent(db_session: AsyncSession) -> None:
    """A second invalidation call for the same observation returns False (already done)."""
    wallet_id = await _insert_wallet(db_session, "0xa003000000000000000000000000000000000003")
    asset_id = await _insert_asset(
        db_session, token_address="0xa004000000000000000000000000000000000004"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    first = await invalidate_observation(db_session, observation_id=obs_id, reason="reorg")
    assert first is True

    second = await invalidate_observation(db_session, observation_id=obs_id, reason="reorg")
    assert second is False

    # Only one record in DB.
    count = (
        await db_session.execute(
            sa.text(
                "SELECT COUNT(*) FROM balance_observation_invalidation WHERE observation_id = :obs"
            ),
            {"obs": str(obs_id)},
        )
    ).scalar()
    assert count == 1


async def test_invalidation_reason_verification_pending(
    db_session: AsyncSession,
) -> None:
    wallet_id = await _insert_wallet(db_session, "0xa005000000000000000000000000000000000005")
    asset_id = await _insert_asset(
        db_session, token_address="0xa006000000000000000000000000000000000006"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    result = await invalidate_observation(
        db_session, observation_id=obs_id, reason="verification_pending"
    )
    assert result is True

    row = (
        await db_session.execute(
            sa.text(
                "SELECT reason FROM balance_observation_invalidation WHERE observation_id = :obs"
            ),
            {"obs": str(obs_id)},
        )
    ).first()
    assert row[0] == "verification_pending"


async def test_invalidation_rejects_unknown_reason(db_session: AsyncSession) -> None:
    wallet_id = await _insert_wallet(db_session, "0xa007000000000000000000000000000000000007")
    asset_id = await _insert_asset(
        db_session, token_address="0xa008000000000000000000000000000000000008"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    with pytest.raises(ValueError, match="invalid invalidation reason"):
        await invalidate_observation(db_session, observation_id=obs_id, reason="bad_reason")


# ---------------------------------------------------------------------------
# Tests: history_point canonicality
# ---------------------------------------------------------------------------


async def test_history_point_marked_non_canonical_when_observation_invalidated(
    db_session: AsyncSession,
) -> None:
    wallet_id = await _insert_wallet(db_session, "0xb001000000000000000000000000000000000009")
    asset_id = await _insert_asset(
        db_session, token_address="0xb002000000000000000000000000000000000010"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
    )
    hp = await materialize_history_point(db_session, snapshot_id=sid)
    assert hp.is_canonical is True

    await invalidate_observation(db_session, observation_id=obs_id, reason="reorg", reorg_depth=5)

    # History point should now be non-canonical.
    row = (
        await db_session.execute(
            sa.text("SELECT is_canonical FROM history_point WHERE id = :id"),
            {"id": str(hp.history_point_id)},
        )
    ).first()
    assert row is not None
    assert row[0] is False


async def test_history_point_stays_canonical_when_unrelated_observation_invalidated(
    db_session: AsyncSession,
) -> None:
    wallet_id = await _insert_wallet(db_session, "0xb003000000000000000000000000000000000011")
    asset_a = await _insert_asset(
        db_session, token_address="0xb004000000000000000000000000000000000012", symbol="AAA"
    )
    asset_b = await _insert_asset(
        db_session, token_address="0xb005000000000000000000000000000000000013", symbol="BBB"
    )
    obs_a = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_a)
    obs_b = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_b)

    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_a,
        observation_id=obs_a,
    )
    hp = await materialize_history_point(db_session, snapshot_id=sid)

    # Invalidate obs_b — not referenced by this snapshot's lines.
    await invalidate_observation(db_session, observation_id=obs_b, reason="reorg")

    row = (
        await db_session.execute(
            sa.text("SELECT is_canonical FROM history_point WHERE id = :id"),
            {"id": str(hp.history_point_id)},
        )
    ).first()
    assert row[0] is True  # unaffected


# ---------------------------------------------------------------------------
# Tests: recheck_canonicality sweep
# ---------------------------------------------------------------------------


async def test_recheck_canonicality_catches_late_invalidations(
    db_session: AsyncSession,
) -> None:
    """recheck_canonicality() detects observations that were invalidated after publish."""
    wallet_id = await _insert_wallet(db_session, "0xc001000000000000000000000000000000000014")
    asset_id = await _insert_asset(
        db_session, token_address="0xc002000000000000000000000000000000000015"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
    )
    hp = await materialize_history_point(db_session, snapshot_id=sid)

    # Manually insert an invalidation record without going through invalidate_observation
    # (to simulate a race condition where the invalidation arrives after publication).
    await db_session.execute(
        sa.text(
            """
            INSERT INTO balance_observation_invalidation
              (id, observation_id, reason, invalidated_at)
            VALUES (:id, :obs, 'reorg', now())
            """
        ),
        {"id": str(uuid.uuid4()), "obs": str(obs_id)},
    )

    count = await recheck_canonicality(db_session)
    assert count == 1

    row = (
        await db_session.execute(
            sa.text("SELECT is_canonical FROM history_point WHERE id = :id"),
            {"id": str(hp.history_point_id)},
        )
    ).first()
    assert row[0] is False


async def test_recheck_canonicality_no_false_positives(
    db_session: AsyncSession,
) -> None:
    """recheck_canonicality() does not flip canonical points with no invalidations."""
    wallet_id = await _insert_wallet(db_session, "0xc003000000000000000000000000000000000016")
    asset_id = await _insert_asset(
        db_session, token_address="0xc004000000000000000000000000000000000017"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
    )
    hp = await materialize_history_point(db_session, snapshot_id=sid)

    count = await recheck_canonicality(db_session)
    assert count == 0

    row = (
        await db_session.execute(
            sa.text("SELECT is_canonical FROM history_point WHERE id = :id"),
            {"id": str(hp.history_point_id)},
        )
    ).first()
    assert row[0] is True


# ---------------------------------------------------------------------------
# Tests: replacement scan scheduling
# ---------------------------------------------------------------------------


async def test_schedule_replacement_scans_returns_affected_pairs(
    db_session: AsyncSession,
) -> None:
    token_addr = "0xd001000000000000000000000000000000000018"
    wallet_id = await _insert_wallet(db_session, "0xd002000000000000000000000000000000000019")
    asset_id = await _insert_asset(db_session, token_address=token_addr)
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
    )
    await materialize_history_point(db_session, snapshot_id=sid)

    await invalidate_observation(db_session, observation_id=obs_id, reason="reorg")

    targets = await schedule_replacement_scans(db_session)
    assert len(targets) == 1
    assert targets[0].wallet_id == wallet_id
    assert targets[0].asset_id == asset_id
    assert targets[0].token_address == token_addr


async def test_schedule_replacement_scans_deduplicates_pairs(
    db_session: AsyncSession,
) -> None:
    """Same (wallet, asset) pair from multiple invalidated observations is deduped."""
    wallet_id = await _insert_wallet(db_session, "0xd003000000000000000000000000000000000020")
    asset_id = await _insert_asset(
        db_session, token_address="0xd004000000000000000000000000000000000021"
    )
    obs_1 = await _insert_balance_observation(
        db_session, wallet_id=wallet_id, asset_id=asset_id, block_number=100
    )
    obs_2 = await _insert_balance_observation(
        db_session, wallet_id=wallet_id, asset_id=asset_id, block_number=101
    )

    # Two snapshots, each using a different observation for the same pair.
    for obs_id in (obs_1, obs_2):
        sid = await _insert_snapshot(db_session)
        await _insert_valuation_line(
            db_session,
            snapshot_id=sid,
            wallet_id=wallet_id,
            asset_id=asset_id,
            observation_id=obs_id,
        )
        await materialize_history_point(db_session, snapshot_id=sid)

    await invalidate_observation(db_session, observation_id=obs_1, reason="reorg")
    await invalidate_observation(db_session, observation_id=obs_2, reason="reorg")

    targets = await schedule_replacement_scans(db_session)
    # Despite two invalidations, only one unique (wallet, asset) pair.
    pairs = {(t.wallet_id, t.asset_id) for t in targets}
    assert len(pairs) == 1


async def test_schedule_replacement_scans_empty_when_all_canonical(
    db_session: AsyncSession,
) -> None:
    wallet_id = await _insert_wallet(db_session, "0xd005000000000000000000000000000000000022")
    asset_id = await _insert_asset(
        db_session, token_address="0xd006000000000000000000000000000000000023"
    )
    obs_id = await _insert_balance_observation(db_session, wallet_id=wallet_id, asset_id=asset_id)

    sid = await _insert_snapshot(db_session)
    await _insert_valuation_line(
        db_session,
        snapshot_id=sid,
        wallet_id=wallet_id,
        asset_id=asset_id,
        observation_id=obs_id,
    )
    await materialize_history_point(db_session, snapshot_id=sid)

    # No invalidations.
    targets = await schedule_replacement_scans(db_session)
    assert targets == []
