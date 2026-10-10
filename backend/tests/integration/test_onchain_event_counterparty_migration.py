"""Integration test for migration 0023 — replacing onchain_event's
from_address/to_address with a single counterparty_address (AUD-389 item 3c).

Runs the real migration chain against a disposable, dedicated database
(created and dropped by this test) rather than the shared test database, so a
failure here cannot corrupt rows other tests in the suite depend on. Mirrors
tests/integration/test_wallet_address_migration.py (AUD-490).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud492"


def _alembic_config() -> Config:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "alembic.ini"
        if candidate.exists():
            return Config(str(candidate))
    raise RuntimeError("alembic.ini not found")


async def _recreate_scratch_db(admin_url: str) -> None:
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": _SCRATCH_DB},
        )
        await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"'))
        await conn.execute(sa.text(f'CREATE DATABASE "{_SCRATCH_DB}"'))
    await admin_engine.dispose()


async def _drop_scratch_db(admin_url: str) -> None:
    admin_engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with admin_engine.connect() as conn:
        await conn.execute(
            sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = :db AND pid <> pg_backend_pid()"
            ),
            {"db": _SCRATCH_DB},
        )
        await conn.execute(sa.text(f'DROP DATABASE IF EXISTS "{_SCRATCH_DB}"'))
    await admin_engine.dispose()


async def test_upgrade_0023_backfills_counterparty_and_drops_owner_columns() -> None:
    """0023 must backfill counterparty_address from whichever side
    event_type says is not the owner, store NULL for a from==to self-transfer
    rather than leaking the owner's own address, and drop from_address/
    to_address together with their lower() CHECK constraints.
    """
    base_url = os.environ["DATABASE_URL"]
    admin_url = base_url
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(admin_url)
    cfg = _alembic_config()

    wallet_id = uuid.uuid4()
    wallet_address = "0x" + "1" * 40
    counterparty_out = "0x" + "2" * 40
    counterparty_in = "0x" + "3" * 40
    spender = "0x" + "4" * 40

    def _tx_hash() -> str:
        return "0x" + uuid.uuid4().hex.ljust(64, "0")

    transfer_out_tx = _tx_hash()
    transfer_in_tx = _tx_hash()
    approval_tx = _tx_hash()
    self_transfer_tx = _tx_hash()

    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0022")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO wallet (id, address_ciphertext, address_bidx,"
                    " label_ciphertext, status)"
                    " VALUES (:id, :ct, :bidx, :label, 'active')"
                ),
                {"id": wallet_id, "ct": b"", "bidx": uuid.uuid4().bytes, "label": b""},
            )
            await conn.execute(
                sa.text(
                    """
                    INSERT INTO onchain_event
                      (wallet_id, tx_hash, block_number, log_index, event_type,
                       token_address, from_address, to_address, raw_amount)
                    VALUES
                      (:wallet_id, :tx_hash, :block_number, 0, :event_type,
                       :token_address, :from_address, :to_address, :raw_amount)
                    """
                ),
                [
                    {
                        "wallet_id": wallet_id,
                        "tx_hash": transfer_out_tx,
                        "block_number": 100,
                        "event_type": "transfer_out",
                        "token_address": "0x" + "a" * 40,
                        "from_address": wallet_address,
                        "to_address": counterparty_out,
                        "raw_amount": "1000",
                    },
                    {
                        "wallet_id": wallet_id,
                        "tx_hash": transfer_in_tx,
                        "block_number": 101,
                        "event_type": "transfer_in",
                        "token_address": "0x" + "a" * 40,
                        "from_address": counterparty_in,
                        "to_address": wallet_address,
                        "raw_amount": "2000",
                    },
                    {
                        "wallet_id": wallet_id,
                        "tx_hash": approval_tx,
                        "block_number": 102,
                        "event_type": "approval",
                        "token_address": "0x" + "a" * 40,
                        "from_address": wallet_address,
                        "to_address": spender,
                        "raw_amount": str(2**256 - 1),
                    },
                    {
                        "wallet_id": wallet_id,
                        "tx_hash": self_transfer_tx,
                        "block_number": 103,
                        "event_type": "transfer_out",
                        "token_address": "0x" + "a" * 40,
                        "from_address": wallet_address,
                        "to_address": wallet_address,
                        "raw_amount": "3000",
                    },
                ],
            )
            await conn.commit()
        await scratch_engine.dispose()

        await asyncio.to_thread(command.upgrade, cfg, "head")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            columns = (
                (
                    await conn.execute(
                        sa.text(
                            "SELECT column_name FROM information_schema.columns"
                            " WHERE table_name = 'onchain_event'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            rows = (
                await conn.execute(
                    sa.text(
                        "SELECT tx_hash, counterparty_address FROM onchain_event"
                        " WHERE wallet_id = :wid"
                    ),
                    {"wid": wallet_id},
                )
            ).all()
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(admin_url)

    assert "from_address" not in columns
    assert "to_address" not in columns
    assert "counterparty_address" in columns

    by_tx = {row.tx_hash: row.counterparty_address for row in rows}
    assert by_tx[transfer_out_tx] == counterparty_out
    assert by_tx[transfer_in_tx] == counterparty_in
    assert by_tx[approval_tx] == spender
    assert by_tx[self_transfer_tx] is None
