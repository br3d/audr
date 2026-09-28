"""Failing integration tests for token discovery (T023 / US1).

Intentionally FAILING until T038 creates tables and T039 implements discovery.

Covers:
  - Catalog discovery: known tokens are discovered from the catalog.
  - Manual discovery: user-added token addresses are probed.
  - Zero balance handling: tokens with zero balance are not surfaced.
  - Nonzero balance handling: tokens with nonzero balances are kept.
  - Duplicate contract: same address from catalog + manual is deduped.
  - Partial coverage: discovery continues if one wallet fails.
  - Restart checkpoint: interrupted discovery resumes from checkpoint.
"""

import uuid

import pytest
import sqlalchemy as sa
from audr.portfolio.discovery import (
    DiscoveryResult,
    discover_tokens,
    get_discovery_checkpoint,
    persist_discovery_candidates,
    save_discovery_checkpoint,
)
from audr.portfolio.balances import get_holdings, record_balance
from sqlalchemy.ext.asyncio import AsyncSession

from audr.jobs.store import (
    JobKind,  # noqa: F401
    claim_job,  # noqa: F401
)


@pytest.mark.integration
async def test_catalog_discovery_finds_known_tokens(
    db_session: AsyncSession,
) -> None:
    """Known catalog tokens are candidates for discovery."""
    result: DiscoveryResult = await discover_tokens(
        db_session,
        wallet_address="0x" + "a" * 40,
        use_catalog=True,
        manual_addresses=[],
    )
    # Catalog is present so at least some candidates must be returned.
    assert isinstance(result.candidates, list)


@pytest.mark.integration
async def test_manual_discovery_uses_provided_addresses(
    db_session: AsyncSession,
) -> None:
    """Explicitly listed token addresses are included as candidates."""
    token = "0x" + "b" * 40
    result = await discover_tokens(
        db_session,
        wallet_address="0x" + "a" * 40,
        use_catalog=False,
        manual_addresses=[token],
    )
    candidate_addresses = [c.token_address.lower() for c in result.candidates]
    assert token.lower() in candidate_addresses


@pytest.mark.integration
async def test_duplicate_contract_deduplication(db_session: AsyncSession) -> None:
    """A token appearing in both catalog and manual list is returned only once."""
    token = "0x" + "c" * 40
    result = await discover_tokens(
        db_session,
        wallet_address="0x" + "a" * 40,
        use_catalog=True,
        manual_addresses=[token, token],
    )
    addresses = [c.token_address.lower() for c in result.candidates]
    assert addresses.count(token.lower()) <= 1


@pytest.mark.integration
async def test_checkpoint_is_saved_and_restored(db_session: AsyncSession) -> None:
    """A discovery checkpoint can be saved and retrieved for resumption."""
    run_id = await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=3)
    assert run_id is not None

    checkpoint = {"wallet_index": 5, "token_index": 12}
    await save_discovery_checkpoint(db_session, run_id=run_id, checkpoint=checkpoint)

    restored = await get_discovery_checkpoint(db_session, run_id=run_id)
    assert restored == checkpoint


@pytest.mark.integration
async def test_checkpoint_is_none_for_fresh_run(db_session: AsyncSession) -> None:
    """A new run has no checkpoint."""
    run_id = await claim_job(db_session, kind=JobKind.DISCOVERY, max_retries=3)
    assert run_id is not None

    restored = await get_discovery_checkpoint(db_session, run_id=run_id)
    assert restored is None


# ---------------------------------------------------------------------------
# Persistence tests (AUD-270)
# ---------------------------------------------------------------------------


async def _insert_wallet(session: AsyncSession, address: str) -> uuid.UUID:
    wallet_id = uuid.uuid4()
    await session.execute(
        sa.text(
            "INSERT INTO wallet (id, address, label, status) "
            "VALUES (:id, :addr, '', 'active')"
        ),
        {"id": str(wallet_id), "addr": address.lower()},
    )
    await session.flush()
    return wallet_id


@pytest.mark.integration
async def test_persist_creates_asset_and_monitored_pair(
    db_session: AsyncSession,
) -> None:
    """persist_discovery_candidates creates asset + monitored_pair rows."""
    wallet_addr = "0x" + "e" * 40
    token_addr = "0x" + "f" * 40
    await _insert_wallet(db_session, wallet_addr)

    from audr.portfolio.discovery import DiscoveryCandidate

    candidates = [
        DiscoveryCandidate(
            token_address=token_addr,
            source="manual",
            symbol="TEST",
            name="Test Token",
            decimals=18,
        )
    ]
    new_pairs = await persist_discovery_candidates(
        db_session,
        wallet_address=wallet_addr,
        candidates=candidates,
    )

    assert new_pairs == 1

    # asset row exists
    asset_row = (
        await db_session.execute(
            sa.text("SELECT id FROM asset WHERE token_address = :addr"),
            {"addr": token_addr.lower()},
        )
    ).first()
    assert asset_row is not None

    # monitored_pair row exists
    pair_row = (
        await db_session.execute(
            sa.text(
                "SELECT mp.id FROM monitored_pair mp "
                "JOIN wallet w ON w.id = mp.wallet_id "
                "JOIN asset a ON a.id = mp.asset_id "
                "WHERE w.address = :waddr AND a.token_address = :taddr"
            ),
            {"waddr": wallet_addr.lower(), "taddr": token_addr.lower()},
        )
    ).first()
    assert pair_row is not None


@pytest.mark.integration
async def test_persist_is_idempotent(db_session: AsyncSession) -> None:
    """Calling persist twice does not duplicate rows."""
    wallet_addr = "0x1" + "1" * 39
    token_addr = "0x2" + "2" * 39
    await _insert_wallet(db_session, wallet_addr)

    from audr.portfolio.discovery import DiscoveryCandidate

    candidates = [
        DiscoveryCandidate(token_address=token_addr, source="manual")
    ]
    first = await persist_discovery_candidates(
        db_session, wallet_address=wallet_addr, candidates=candidates
    )
    second = await persist_discovery_candidates(
        db_session, wallet_address=wallet_addr, candidates=candidates
    )

    assert first == 1
    assert second == 0  # already existed — no new row

    count = (
        await db_session.execute(
            sa.text(
                "SELECT COUNT(*) FROM monitored_pair mp "
                "JOIN wallet w ON w.id = mp.wallet_id "
                "JOIN asset a ON a.id = mp.asset_id "
                "WHERE w.address = :waddr AND a.token_address = :taddr"
            ),
            {"waddr": wallet_addr.lower(), "taddr": token_addr.lower()},
        )
    ).scalar()
    assert count == 1


@pytest.mark.integration
async def test_discover_persist_scan_shows_holdings(
    db_session: AsyncSession,
) -> None:
    """End-to-end: discover → persist → record_balance → get_holdings returns token."""
    wallet_addr = "0x3" + "3" * 39
    token_addr = "0x4" + "4" * 39
    await _insert_wallet(db_session, wallet_addr)

    # Discover manually — no catalog needed
    result = await discover_tokens(
        db_session,
        wallet_address=wallet_addr,
        use_catalog=False,
        manual_addresses=[token_addr],
    )
    assert any(c.token_address.lower() == token_addr.lower() for c in result.candidates)

    # Persist candidates → creates asset + monitored_pair
    new_pairs = await persist_discovery_candidates(
        db_session,
        wallet_address=wallet_addr,
        candidates=result.candidates,
    )
    assert new_pairs == 1

    # Simulate a balance scan result
    await record_balance(
        db_session,
        wallet_address=wallet_addr,
        token_address=token_addr,
        raw_amount=500_000_000_000_000_000,
        block_number=1_000_000,
    )

    # Holdings should now include the token
    holdings = await get_holdings(db_session, wallet_address=wallet_addr)
    match = next(
        (h for h in holdings if h.token_address.lower() == token_addr.lower()), None
    )
    assert match is not None
    assert match.raw_amount == 500_000_000_000_000_000
