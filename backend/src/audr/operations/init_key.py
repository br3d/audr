"""One-shot master-key initialisation and retrieval (T012).

The master key is a randomly-generated 32-byte AES-256 key that is generated on
first call to ``init_key`` and stored in the ``key_state`` table as an
encrypted blob (wrapped with a key-encryption-key derived from the env SECRET_KEY).

Usage::

    async with async_session() as session:
        await init_key(session)          # idempotent — safe to call on every startup
        key = await get_master_key(session)  # 32-byte AES key

Rotating the key-encryption-key (e.g. because SECRET_KEY leaked) does not
require re-encrypting every credential: ``rotate_kek`` re-wraps the stored
master key under a new KEK while leaving the raw master key — and therefore
every ciphertext it protects — unchanged. See ``python -m
audr.operations.init_key rotate`` (``_main_rotate``) for the one-shot CLI form.

The ``key_state`` table is created by the 0001 baseline migration (originally T017).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import sys

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.crypto import InvalidEnvelopeError, MissingKeyError, decrypt, encrypt

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
        raise MissingKeyError("master key has not been initialised; call init_key() first")
    return decrypt(blob, b"key_state:master_key", kek)


async def rotate_kek(session: AsyncSession, old_kek: bytes, new_kek: bytes) -> None:
    """Rewrap the stored master-key blob under *new_kek*, in place.

    The raw master key value is never changed — only the key-encryption-key
    that wraps it — so every ciphertext encrypted under the master key
    (integration credentials, etc.) stays decryptable without re-encryption.

    Idempotent: if the blob already decrypts under *new_kek* (e.g. a prior
    call succeeded and this is a re-run), returns without touching the row.

    Raises:
        MissingKeyError: no master key has been initialised yet.
        InvalidEnvelopeError / MissingKeyError: *old_kek* does not unwrap the
            stored blob (and it is not already wrapped under *new_kek*). The
            row is left untouched.
    """
    existing = await _load_raw(session)
    if existing is None:
        raise MissingKeyError("master key has not been initialised; call init_key() first")

    try:
        decrypt(existing, b"key_state:master_key", new_kek)
    except InvalidEnvelopeError:
        pass
    else:
        return

    raw_key = decrypt(existing, b"key_state:master_key", old_kek)
    wrapped = encrypt(raw_key, b"key_state:master_key", new_kek)
    await session.execute(
        text("UPDATE key_state SET wrapped_key = :blob WHERE name = :name"),
        {"blob": wrapped, "name": _KEY_STATE_ROW},
    )
    await session.flush()


async def _load_raw(session: AsyncSession) -> bytes | None:
    result = await session.execute(
        text("SELECT wrapped_key FROM key_state WHERE name = :name"),
        {"name": _KEY_STATE_ROW},
    )
    row = result.first()
    return row[0] if row is not None else None


def _load_kek(env_var: str = "SECRET_KEY") -> bytes:
    """Load a key-encryption-key from the given env var.

    The value must be a 64-character hex string (32 bytes).  Raises
    MissingKeyError if absent or the wrong length.
    """
    raw = os.environ.get(env_var, "")
    if not raw:
        raise MissingKeyError(f"{env_var} environment variable is not set")
    try:
        kek = bytes.fromhex(raw)
    except ValueError as exc:
        raise MissingKeyError(f"{env_var} is not valid hexadecimal") from exc
    if len(kek) != 32:
        raise MissingKeyError(f"{env_var} must be 32 bytes (64 hex chars), got {len(kek)}")
    return kek


def _kek_fingerprint(kek: bytes) -> str:
    """Non-reversible identifier for a KEK, safe to print: sha256 prefix, not the key."""
    return hashlib.sha256(kek).hexdigest()[:12]


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


async def _main_rotate() -> None:
    """One-shot KEK rotation entrypoint.

    Usage inside the audr-api image.  Pass the two keys in a file rather than
    as ``-e VAR=<value>`` arguments: inline values land in the operator's shell
    history and are readable in ``ps`` output for the life of the container,
    which is the exact leak class AUD-428 exists to clean up.

        umask 077 && cat > rotate.env <<'EOF'
        OLD_SECRET_KEY=...
        NEW_SECRET_KEY=...
        EOF
        docker compose run --rm --env-file rotate.env \\
            migrate python -m audr.operations.init_key rotate
        shred -u rotate.env

    Reads OLD_SECRET_KEY / NEW_SECRET_KEY (not SECRET_KEY) so the rotation
    cannot be run by accident with only one key configured, and prints only a
    sha256 fingerprint of the new KEK — never a key value — on success.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    old_kek = _load_kek("OLD_SECRET_KEY")
    new_kek = _load_kek("NEW_SECRET_KEY")

    database_url = os.environ["DATABASE_URL"]
    engine = create_async_engine(database_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        await rotate_kek(session, old_kek, new_kek)
        await session.commit()
    await engine.dispose()
    print(f"Master key rewrapped under new KEK (fingerprint {_kek_fingerprint(new_kek)}).")


if __name__ == "__main__":
    # Reject an unrecognised argument instead of falling through to init: a
    # mistyped `rotate` would otherwise run the idempotent initialiser, print
    # "Master key initialised." and exit 0, which during a maintenance window
    # reads as a successful rotation that never happened.
    if len(sys.argv) > 2 or (len(sys.argv) == 2 and sys.argv[1] != "rotate"):
        print("usage: python -m audr.operations.init_key [rotate]", file=sys.stderr)
        raise SystemExit(2)
    if len(sys.argv) == 2:
        asyncio.run(_main_rotate())
    else:
        asyncio.run(_main())
