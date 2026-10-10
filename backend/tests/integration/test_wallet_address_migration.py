"""Integration test for migration 0022 — encrypting pre-existing plaintext
wallet addresses behind a unique HMAC blind index (AUD-490).

Runs the real migration chain against a disposable, dedicated database
(created and dropped by this test) rather than the shared test database, so a
failure here cannot corrupt wallet rows other tests in the suite depend on.
Mirrors tests/integration/test_wallet_label_migration.py (AUD-488).
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

from audr.operations.crypto import decrypt
from audr.wallets.service import compute_address_bidx

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud490"


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


async def test_upgrade_0022_encrypts_preexisting_plaintext_addresses(
    test_secret_key: str,
) -> None:
    """0022 must encrypt whatever plaintext addresses already exist, move the
    unique constraint to the blind index, and drop the plaintext column.

    Upgrades a fresh database to 0021 (the schema immediately before this
    one), inserts a wallet the way pre-AUD-490 code would have (plaintext
    `address` column), upgrades to head, and confirms the stored envelope
    decrypts back to the exact original address and that the stored blind
    index matches what `compute_address_bidx` would compute for it — the
    on-disk format the service layer reads must match what this migration
    actually wrote.
    """
    base_url = os.environ["DATABASE_URL"]
    admin_url = base_url  # any existing database on the server will do for CREATE/DROP DATABASE
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(admin_url)

    cfg = _alembic_config()
    wallet_id = uuid.uuid4()
    address = "0x" + "7" * 40

    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0021")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO wallet (id, address, label_ciphertext, status)"
                    " VALUES (:id, :addr, '', 'active')"
                ),
                {"id": wallet_id, "addr": address},
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
                            " WHERE table_name = 'wallet'"
                        )
                    )
                )
                .scalars()
                .all()
            )
            row = (
                await conn.execute(
                    sa.text("SELECT address_ciphertext, address_bidx FROM wallet WHERE id = :id"),
                    {"id": wallet_id},
                )
            ).one()
            wrapped_key = (
                await conn.execute(
                    sa.text("SELECT wrapped_key FROM key_state WHERE name = 'master_key'")
                )
            ).scalar_one()
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(admin_url)

    assert "address" not in columns
    assert "address_ciphertext" in columns
    assert "address_bidx" in columns

    kek = bytes.fromhex(test_secret_key)
    master_key = decrypt(wrapped_key, b"key_state:master_key", kek)
    recovered_address = decrypt(
        row.address_ciphertext, f"wallet:address:{wallet_id}".encode(), master_key
    ).decode("utf-8")
    assert recovered_address == address

    assert bytes(row.address_bidx) == compute_address_bidx(address, master_key)


async def test_upgrade_0022_rejects_duplicate_address_via_bidx(
    test_secret_key: str,
) -> None:
    """The unique constraint that used to sit on `address` now sits on
    `address_bidx`. The 0001 baseline's CHECK already forced `address` to be
    stored lowercase, so two pre-existing rows cannot literally collide on
    the raw column — but a row inserted by hand bypassing that CHECK (e.g. a
    mixed-case value some out-of-band script wrote) and a normal lowercase
    row for the same address must still collide after the upgrade, since both
    migrate to the same lowercase-normalised blind index. This is the
    scenario the migration's uniqueness guarantee exists to catch.
    """
    base_url = os.environ["DATABASE_URL"]
    admin_url = base_url
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(admin_url)
    cfg = _alembic_config()

    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0021")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            await conn.execute(
                sa.text("ALTER TABLE wallet DROP CONSTRAINT ck_wallet_ck_wallet_address_lower")
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO wallet (id, address, label_ciphertext, status)"
                    " VALUES (:id, :addr, '', 'active')"
                ),
                {"id": uuid.uuid4(), "addr": "0x" + "9" * 40},
            )
            await conn.execute(
                sa.text(
                    "INSERT INTO wallet (id, address, label_ciphertext, status)"
                    " VALUES (:id, :addr, '', 'active')"
                ),
                {"id": uuid.uuid4(), "addr": "0X" + "9" * 40},
            )
            await conn.commit()
        await scratch_engine.dispose()

        with pytest.raises(Exception, match="(?i)unique|duplicate"):
            await asyncio.to_thread(command.upgrade, cfg, "head")
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(admin_url)
