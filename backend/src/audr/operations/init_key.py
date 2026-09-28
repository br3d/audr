"""One-shot master-key initialisation and retrieval (T012).

The master key is a randomly-generated 32-byte AES-256 key that is generated on
first call to ``init_key`` and stored in the ``key_state`` table as an
encrypted blob (wrapped with a key-encryption-key derived from the env SECRET_KEY).

Usage::

    async with async_session() as session:
        await init_key(session)          # idempotent — safe to call on every startup
        key = await get_master_key(session)  # 32-byte AES key

The ``key_state`` table is created by the 0001 baseline migration (originally T017).
"""

from __future__ import annotations

import asyncio
import os

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.crypto import MissingKeyError, decrypt, encrypt

# Row key used in the key_state table.
_KEY_STATE_ROW = "master_key"


async def init_key(session: AsyncSession) -> None:
    """Initialise or validate the master key in the database.

    - If no key exists: generates 32 random bytes, wraps them, and inserts a row.
    - If a row already exists: verifies the stored blob can be unwrapped (idempotent).
    - If SECRET_KEY env var is missing or wrong length: raises MissingKeyError.
    """
    kek = _load_kek()
    existing = await _load_raw(session)
    if existing is None:
        raw_key = os.urandom(32)
        wrapped = encrypt(raw_key, b"key_state:master_key", kek)
        await session.execute(
            text(
                "INSERT INTO key_state (name, wrapped_key) VALUES (:name, :blob)"
                " ON CONFLICT (name) DO NOTHING"
            ),
            {"name": _KEY_STATE_ROW, "blob": wrapped},
        )
        await session.flush()
    else:
        # Validate that the KEK can unwrap the stored blob (raises on mismatch).
        decrypt(existing, b"key_state:master_key", kek)


async def get_master_key(session: AsyncSession) -> bytes:
    """Return the 32-byte master key, unwrapping it from the database.

    Raises MissingKeyError if init_key has not been called yet.
    """
    kek = _load_kek()
    blob = await _load_raw(session)
    if blob is None:
        raise MissingKeyError(
            "master key has not been initialised; call init_key() first"
        )
    return decrypt(blob, b"key_state:master_key", kek)


async def _load_raw(session: AsyncSession) -> bytes | None:
    result = await session.execute(
        text("SELECT wrapped_key FROM key_state WHERE name = :name"),
        {"name": _KEY_STATE_ROW},
    )
    row = result.first()
    return row[0] if row is not None else None


def _load_kek() -> bytes:
    """Load the key-encryption-key from the SECRET_KEY env var.

    SECRET_KEY must be a 64-character hex string (32 bytes).  Raises
    MissingKeyError if absent or the wrong length.
    """
    raw = os.environ.get("SECRET_KEY", "")
    if not raw:
        raise MissingKeyError("SECRET_KEY environment variable is not set")
    try:
        kek = bytes.fromhex(raw)
    except ValueError as exc:
        raise MissingKeyError("SECRET_KEY is not valid hexadecimal") from exc
    if len(kek) != 32:
        raise MissingKeyError(
            f"SECRET_KEY must be 32 bytes (64 hex chars), got {len(kek)}"
        )
    return kek


async def _main() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await init_key(session)
        await session.commit()
    await engine.dispose()
    print("Master key initialised.")


if __name__ == "__main__":
    asyncio.run(_main())
