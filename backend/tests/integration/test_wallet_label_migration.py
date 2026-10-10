"""Integration test for migration 0021 — encrypting pre-existing plaintext
wallet labels (AUD-488).

Runs the real migration chain against a disposable, dedicated database
(created and dropped by this test) rather than the shared test database, so a
failure here cannot corrupt wallet rows other tests in the suite depend on.
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

pytestmark = pytest.mark.integration

_SCRATCH_DB = "audr_migration_scratch_aud488"


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


async def test_upgrade_0021_encrypts_preexisting_plaintext_labels(
    test_secret_key: str,
) -> None:
    """0021 must encrypt whatever plaintext labels already exist.

    Upgrades a fresh database to 0020 (the schema immediately before this
    one), inserts a wallet the way pre-AUD-488 code would have (plaintext
    `label` column), upgrades to head, and confirms the stored envelope
    decrypts back to the exact original string — the "must not show
    mojibake or empty labels" requirement from the issue — and that the
    plaintext `label` column is gone in favour of `label_ciphertext`.
    """
    base_url = os.environ["DATABASE_URL"]
    admin_url = base_url  # any existing database on the server will do for CREATE/DROP DATABASE
    scratch_url = base_url.rsplit("/", 1)[0] + f"/{_SCRATCH_DB}"

    await _recreate_scratch_db(admin_url)

    cfg = _alembic_config()
    wallet_id = uuid.uuid4()
    plaintext_label = "Vitalik's cold wallet — café"
    address = "0x" + "9" * 40

    old_db_url = os.environ["DATABASE_URL"]
    os.environ["DATABASE_URL"] = scratch_url
    try:
        await asyncio.to_thread(command.upgrade, cfg, "0020")

        scratch_engine = create_async_engine(scratch_url)
        async with scratch_engine.connect() as conn:
            await conn.execute(
                sa.text(
                    "INSERT INTO wallet (id, address, label, status)"
                    " VALUES (:id, :addr, :label, 'active')"
                ),
                {"id": wallet_id, "addr": address, "label": plaintext_label},
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
            ciphertext = (
                await conn.execute(
                    sa.text("SELECT label_ciphertext FROM wallet WHERE id = :id"),
                    {"id": wallet_id},
                )
            ).scalar_one()
            wrapped_key = (
                await conn.execute(
                    sa.text("SELECT wrapped_key FROM key_state WHERE name = 'master_key'")
                )
            ).scalar_one()
        await scratch_engine.dispose()
    finally:
        os.environ["DATABASE_URL"] = old_db_url
        await _drop_scratch_db(admin_url)

    assert "label" not in columns
    assert "label_ciphertext" in columns

    kek = bytes.fromhex(test_secret_key)
    master_key = decrypt(wrapped_key, b"key_state:master_key", kek)
    recovered_label = decrypt(ciphertext, f"wallet:label:{wallet_id}".encode(), master_key).decode(
        "utf-8"
    )
    assert recovered_label == plaintext_label
