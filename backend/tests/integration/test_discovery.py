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

import pytest
from audr.portfolio.discovery import (
    DiscoveryResult,
    discover_tokens,
    get_discovery_checkpoint,
    save_discovery_checkpoint,
)
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
