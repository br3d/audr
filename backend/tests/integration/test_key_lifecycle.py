"""Integration tests for the master-key initialization lifecycle.

These tests exercise audr.operations.init_key against a live PostgreSQL
database (the key_state table, created by the 0001 baseline migration).  They
also verify the full encrypt→decrypt round-trip through the managed key.

Requires (via fixtures in tests/conftest.py):
  - TEST_DATABASE_URL pointing at a test PostgreSQL instance.
  - test_secret_key fixture (sets SECRET_KEY to a safe test value).
  - key_state table existing (run `alembic upgrade head` first).

Marker: integration
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.crypto import (
    InvalidEnvelopeError,
    MissingKeyError,
    decrypt,
    encrypt,
)
from audr.operations.init_key import get_master_key, init_key, rotate_kek
from audr.settings.integrations import get_integration, upsert_integration


@pytest.fixture(autouse=True)
async def _clean_key_state(db_session: AsyncSession) -> None:
    """Remove any existing key_state rows so each test starts fresh.

    The db_session fixture (from root conftest) wraps each test in a rollback,
    so this cleanup is belt-and-suspenders for explicit teardown visibility.
    """
    await db_session.execute(text("DELETE FROM key_state"))
    await db_session.flush()
    yield


@pytest.mark.integration
async def test_get_master_key_raises_before_init(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """get_master_key must raise MissingKeyError when the key_state row is absent."""
    with pytest.raises(MissingKeyError):
        await get_master_key(db_session)


@pytest.mark.integration
async def test_init_key_stores_a_32_byte_key(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """After init_key, get_master_key returns a 32-byte key."""
    await init_key(db_session)
    key = await get_master_key(db_session)
    assert isinstance(key, bytes)
    assert len(key) == 32


@pytest.mark.integration
async def test_master_key_is_stable_across_calls(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """Repeated calls to get_master_key return the same value within a session."""
    await init_key(db_session)
    key1 = await get_master_key(db_session)
    key2 = await get_master_key(db_session)
    assert key1 == key2


@pytest.mark.integration
async def test_init_key_is_idempotent(db_session: AsyncSession, test_secret_key: str) -> None:
    """Calling init_key twice must not change the stored key or raise an error."""
    await init_key(db_session)
    key_first = await get_master_key(db_session)
    await init_key(db_session)
    key_second = await get_master_key(db_session)
    assert key_first == key_second


@pytest.mark.integration
async def test_master_key_is_random_not_a_constant(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """The generated key must be random (not all-zeros, not derived only from KEK)."""
    await init_key(db_session)
    key = await get_master_key(db_session)
    assert key != b"\x00" * 32
    assert key != b"\xff" * 32


@pytest.mark.integration
async def test_missing_secret_key_raises_on_init(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If SECRET_KEY is absent, init_key must raise MissingKeyError immediately."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(MissingKeyError):
        await init_key(db_session)


@pytest.mark.integration
async def test_missing_secret_key_raises_on_get(
    db_session: AsyncSession, test_secret_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_master_key with a missing SECRET_KEY must raise MissingKeyError."""
    await init_key(db_session)
    monkeypatch.delenv("SECRET_KEY", raising=False)
    with pytest.raises(MissingKeyError):
        await get_master_key(db_session)


@pytest.mark.integration
async def test_wrong_kek_cannot_unwrap_stored_key(
    db_session: AsyncSession, test_secret_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_master_key with a rotated SECRET_KEY must fail authentication."""
    await init_key(db_session)
    monkeypatch.setenv("SECRET_KEY", os.urandom(32).hex())
    with pytest.raises((MissingKeyError, InvalidEnvelopeError)):
        await get_master_key(db_session)


@pytest.mark.integration
async def test_encrypt_decrypt_round_trip_with_managed_key(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """End-to-end: init_key → get_master_key → encrypt → decrypt recovers plaintext."""
    await init_key(db_session)
    key = await get_master_key(db_session)

    plaintext = b"https://mainnet.infura.io/v3/secret"
    aad = b"rpc_url:7"
    ciphertext = encrypt(plaintext, aad, key)
    assert decrypt(ciphertext, aad, key) == plaintext


@pytest.mark.integration
async def test_record_bound_aad_enforced_end_to_end(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """A credential encrypted for rpc_url:7 must not decrypt as rpc_url:8."""
    await init_key(db_session)
    key = await get_master_key(db_session)

    ciphertext = encrypt(b"https://mainnet.infura.io/v3/secret", b"rpc_url:7", key)
    with pytest.raises(InvalidEnvelopeError):
        decrypt(ciphertext, b"rpc_url:8", key)


@pytest.mark.integration
async def test_two_encryptions_of_same_plaintext_are_distinct(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """Each encrypt call uses a fresh nonce — identical plaintext yields distinct blobs."""
    await init_key(db_session)
    key = await get_master_key(db_session)
    ct1 = encrypt(b"https://mainnet.infura.io/v3/secret", b"rpc_url:1", key)
    ct2 = encrypt(b"https://mainnet.infura.io/v3/secret", b"rpc_url:1", key)
    assert ct1 != ct2


# ---------------------------------------------------------------------------
# KEK rotation (AUD-428)
# ---------------------------------------------------------------------------

# Synthetic 32-byte KEKs for rotation tests — never real values.
_NEW_KEK_HEX = "c" * 64
_WRONG_OLD_KEK_HEX = "d" * 64


@pytest.mark.integration
async def test_rotate_kek_preserves_master_key_value(
    db_session: AsyncSession, test_secret_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rotate_kek rewraps the blob under a new KEK without changing the raw master key."""
    await init_key(db_session)
    key_before = await get_master_key(db_session)

    old_kek = bytes.fromhex(test_secret_key)
    new_kek = bytes.fromhex(_NEW_KEK_HEX)
    await rotate_kek(db_session, old_kek, new_kek)

    monkeypatch.setenv("SECRET_KEY", _NEW_KEK_HEX)
    key_after = await get_master_key(db_session)

    assert key_after == key_before


@pytest.mark.integration
async def test_rotate_kek_is_idempotent(
    db_session: AsyncSession, test_secret_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running rotate_kek with the same old/new pair after a successful rotation is a no-op."""
    await init_key(db_session)
    old_kek = bytes.fromhex(test_secret_key)
    new_kek = bytes.fromhex(_NEW_KEK_HEX)

    await rotate_kek(db_session, old_kek, new_kek)
    monkeypatch.setenv("SECRET_KEY", _NEW_KEK_HEX)
    key_after_first = await get_master_key(db_session)

    # Second call: the blob is already wrapped under new_kek, so old_kek no
    # longer unwraps it. A naive implementation would try old_kek and raise;
    # the no-op path must detect new_kek already works and skip straight past.
    await rotate_kek(db_session, old_kek, new_kek)
    key_after_second = await get_master_key(db_session)

    assert key_after_first == key_after_second


@pytest.mark.integration
async def test_rotate_kek_wrong_old_kek_raises_and_leaves_blob_intact(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """A wrong old KEK must raise instead of writing a corrupt blob."""
    await init_key(db_session)
    key_before = await get_master_key(db_session)

    wrong_old_kek = bytes.fromhex(_WRONG_OLD_KEK_HEX)
    new_kek = bytes.fromhex(_NEW_KEK_HEX)
    with pytest.raises((InvalidEnvelopeError, MissingKeyError)):
        await rotate_kek(db_session, wrong_old_kek, new_kek)

    # The blob must still be readable under the original (still-correct) KEK.
    key_after = await get_master_key(db_session)
    assert key_after == key_before


@pytest.mark.integration
async def test_rotate_kek_raises_when_master_key_not_initialised(
    db_session: AsyncSession, test_secret_key: str
) -> None:
    """rotate_kek on an uninitialised key_state row must raise, not create one."""
    old_kek = bytes.fromhex(test_secret_key)
    new_kek = bytes.fromhex(_NEW_KEK_HEX)
    with pytest.raises(MissingKeyError):
        await rotate_kek(db_session, old_kek, new_kek)


@pytest.mark.integration
async def test_rotate_kek_preserves_integration_credential_decryption(
    db_session: AsyncSession, test_secret_key: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Integration credentials encrypted under the old master-key wrapping stay readable."""
    await init_key(db_session)
    await upsert_integration(
        db_session,
        kind="rpc",
        url="https://rpc.invalid.example/v3/test",
        api_key="test-api-key",
    )

    old_kek = bytes.fromhex(test_secret_key)
    new_kek = bytes.fromhex(_NEW_KEK_HEX)
    await rotate_kek(db_session, old_kek, new_kek)
    monkeypatch.setenv("SECRET_KEY", _NEW_KEK_HEX)

    read_back = await get_integration(db_session, kind="rpc", decrypt_fields=True)
    assert read_back is not None
    assert read_back.url == "https://rpc.invalid.example/v3/test"
    assert read_back.api_key == "test-api-key"
