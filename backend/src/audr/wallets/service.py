"""Wallet management service — add, label, stop, reactivate (T032 / US1)."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from audr.operations.crypto import InvalidEnvelopeError, MissingKeyError, decrypt, encrypt
from audr.operations.init_key import get_master_key
from audr.portfolio.history import rematerialize_history_points
from audr.wallets.models import Wallet


class WalletAlreadyExistsError(Exception):
    """Raised when adding a wallet with an address that is already tracked."""


class WalletNotFoundError(Exception):
    """Raised when a wallet_id does not exist."""


class InvalidAddressError(Exception):
    """Raised when the provided Ethereum address is malformed."""


# A label envelope's AAD is bound to its wallet id, so it cannot be moved onto
# another row even by someone with write access to the table (AUD-488).
_LABEL_AAD_PREFIX = "wallet:label:"

# Same idea for the address envelope, with a distinct prefix so an envelope
# cannot be moved between the two columns either (AUD-490).
_ADDRESS_AAD_PREFIX = "wallet:address:"

# Fixed HKDF info label for deriving the blind-index subkey from the master
# key. A distinct subkey (not the raw master key) is used for the HMAC blind
# index so that compromising the index key alone does not help decrypt
# address/label envelopes, and vice versa (AUD-490).
_BIDX_HKDF_INFO = b"audr:wallet:address_bidx:v1"

# Returned in place of a label whose envelope fails to decrypt (wrong/rotated
# key, or a corrupted row) rather than raising. See the key-loss rationale in
# docs/operations.md#key-loss-behavior: a single unreadable label must not
# turn GET /wallets into a 500 for every wallet in the list.
LABEL_UNREADABLE_PLACEHOLDER = "[unreadable]"


def _label_aad(wallet_id: uuid.UUID) -> bytes:
    return f"{_LABEL_AAD_PREFIX}{wallet_id}".encode()


def _address_aad(wallet_id: uuid.UUID) -> bytes:
    return f"{_ADDRESS_AAD_PREFIX}{wallet_id}".encode()


def encrypt_label(label: str, wallet_id: uuid.UUID, key: bytes) -> bytes:
    """Encrypt a wallet label. Exposed for callers that insert a Wallet row
    directly (e.g. audr.portfolio.balances._ensure_wallet) rather than going
    through add_wallet."""
    return encrypt(label.encode("utf-8"), _label_aad(wallet_id), key)


def decrypt_label(ciphertext: bytes, wallet_id: uuid.UUID, key: bytes | None) -> str:
    if key is None:
        return LABEL_UNREADABLE_PLACEHOLDER
    try:
        return decrypt(ciphertext, _label_aad(wallet_id), key).decode("utf-8")
    except (InvalidEnvelopeError, UnicodeDecodeError):
        return LABEL_UNREADABLE_PLACEHOLDER


def encrypt_address(address: str, wallet_id: uuid.UUID, key: bytes) -> bytes:
    """Encrypt a normalised wallet address. Exposed for callers that insert a
    Wallet row directly (e.g. audr.portfolio.balances._ensure_wallet) rather
    than going through add_wallet."""
    return encrypt(address.encode("utf-8"), _address_aad(wallet_id), key)


def decrypt_address(ciphertext: bytes, wallet_id: uuid.UUID, key: bytes) -> str:
    """Decrypt a wallet address envelope.

    Unlike labels, a failure here is not papered over with a placeholder: an
    address that cannot be decrypted means the wallet it belongs to cannot be
    identified at all, which is a harder failure than an unreadable label (see
    docs/operations.md#key-loss-behavior). Callers let MissingKeyError /
    InvalidEnvelopeError propagate.
    """
    return decrypt(ciphertext, _address_aad(wallet_id), key).decode("utf-8")


def _derive_bidx_key(master_key: bytes) -> bytes:
    """Derive the blind-index subkey from the master key via HKDF-SHA256."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=_BIDX_HKDF_INFO).derive(
        master_key
    )


def compute_address_bidx(normalised_address: str, master_key: bytes) -> bytes:
    """Deterministic HMAC-SHA256 blind index for a normalised (lowercase) address.

    Deterministic so the same address always produces the same index value
    (needed for the unique constraint and for equality lookups), but keyed by
    a subkey derived from the master key rather than reproducible from the
    address alone.
    """
    subkey = _derive_bidx_key(master_key)
    return hmac.new(subkey, normalised_address.encode("utf-8"), hashlib.sha256).digest()


async def _attach_wallet_fields(session: AsyncSession, wallets: list[Wallet]) -> None:
    """Decrypt and attach the transient ``.address`` / ``.label`` attributes.

    The master key is fetched once via ``get_master_key``, which raises
    (MissingKeyError / InvalidEnvelopeError) rather than degrading — an
    address that cannot be decrypted makes the wallet list itself unusable,
    unlike a single bad label (see docs/operations.md#key-loss-behavior).
    """
    if not wallets:
        return
    key = await get_master_key(session)
    for wallet in wallets:
        wallet.address = decrypt_address(wallet.address_ciphertext, wallet.id, key)
        wallet.label = decrypt_label(wallet.label_ciphertext, wallet.id, key)


def _normalize_address(address: str) -> str:
    """Lowercase and basic-validate an Ethereum address."""
    stripped = address.strip()
    if not stripped.startswith("0x") or len(stripped) != 42:
        raise InvalidAddressError(f"Invalid Ethereum address: {address!r}")
    try:
        int(stripped, 16)
    except ValueError as exc:
        raise InvalidAddressError(f"Invalid Ethereum address: {address!r}") from exc
    return stripped.lower()


async def add_wallet(
    session: AsyncSession,
    *,
    address: str,
    label: str = "",
) -> Wallet:
    """Add a new tracked wallet.  Raises WalletAlreadyExistsError on duplicate."""
    normalised = _normalize_address(address)
    wallet_id = uuid.uuid4()
    key = await get_master_key(session)
    wallet = Wallet(
        id=wallet_id,
        address_ciphertext=encrypt_address(normalised, wallet_id, key),
        address_bidx=compute_address_bidx(normalised, key),
        label_ciphertext=encrypt_label(label, wallet_id, key),
        status="active",
    )
    wallet.address = normalised
    wallet.label = label
    session.add(wallet)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise WalletAlreadyExistsError(normalised) from exc
    await session.commit()
    return wallet


async def set_label(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
    label: str,
) -> Wallet:
    """Update the label of a wallet."""
    wallet = await _get_or_raise(session, wallet_id)
    key = await get_master_key(session)
    wallet.label_ciphertext = encrypt_label(label, wallet_id, key)
    wallet.label = label
    wallet.updated_at = datetime.now(tz=UTC)
    await session.flush()
    await session.commit()
    return wallet


async def stop_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> Wallet:
    """Stop balance scanning for a wallet."""
    wallet = await _get_or_raise(session, wallet_id)
    wallet.status = "stopped"
    wallet.updated_at = datetime.now(tz=UTC)
    await session.flush()
    await session.commit()
    return wallet


async def reactivate_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> Wallet:
    """Resume balance scanning for a wallet."""
    wallet = await _get_or_raise(session, wallet_id)
    wallet.status = "active"
    wallet.updated_at = datetime.now(tz=UTC)
    await session.flush()
    await session.commit()
    return wallet


# A valuation line belongs to the wallet either directly or through the balance
# observation it was priced from; both reach the wallet, and the observation arm
# keeps a line whose wallet_id was somehow not set from blocking the delete.
_OWNED_VALUATION_LINE = (
    "(wallet_id = :wid OR observation_id IN ("
    "   SELECT id FROM balance_observation WHERE wallet_id = :wid"
    " ))"
)


async def delete_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> dict[str, int]:
    """Permanently delete a wallet and every record derived from it (AUD-367).

    Unlike :func:`stop_wallet`, which only pauses scanning, this removes the
    address from the application entirely: balance observations, discovery
    coverage, monitored pairs, indexed on-chain events, the event-indexer
    checkpoint, and the wallet's valuation lines all go.  Valuation snapshots
    (and their history points) left without any line are removed too, so the
    history series does not keep reporting a total that included this wallet.

    Shared records that are not owned by the wallet — ``asset``, catalogue
    entries, quote sets — are left untouched.

    Returns a mapping of table name to deleted row count, for the audit trail.
    Raises WalletNotFoundError if the wallet does not exist.
    """
    await _get_or_raise(session, wallet_id)
    params: dict[str, object] = {"wid": str(wallet_id)}
    deleted: dict[str, int] = {}

    async def _delete(table: str, sql: str) -> None:
        stmt = sa.text(sql)
        if ":snapshot_ids" in sql:
            # Expanding bindparam renders a literal IN (...) list, which keeps
            # asyncpg from having to infer an array parameter type.
            stmt = stmt.bindparams(sa.bindparam("snapshot_ids", expanding=True))
        result = await session.execute(stmt, params)
        deleted[table] = int(result.rowcount or 0)

    # Snapshots this wallet contributed to, captured before its lines go: after
    # the delete there is no way left to tell them apart from snapshots that
    # never included this address.
    touched_snapshots = list(
        (
            # _OWNED_VALUATION_LINE is a fixed fragment; its value is bound, not interpolated.
            await session.execute(
                sa.text(
                    "SELECT DISTINCT snapshot_id FROM valuation_line"  # noqa: S608 — _OWNED_VALUATION_LINE is a fixed ":param" fragment, not interpolated
                    f" WHERE {_OWNED_VALUATION_LINE}"
                ),
                params,
            )
        ).scalars()
    )
    params["snapshot_ids"] = touched_snapshots

    # valuation_line references balance_observation as well as wallet, so it has
    # to go before the observations it was priced from — deleting observations
    # first trips fk_valuation_line_observation_id_balance_observation (AUD-394).
    await _delete(
        "valuation_line",
        f"DELETE FROM valuation_line WHERE {_OWNED_VALUATION_LINE}",  # noqa: S608 -- fixed ":param" fragment
    )
    # Child of balance_observation — must go before its parent.
    await _delete(
        "balance_observation_invalidation",
        "DELETE FROM balance_observation_invalidation"
        " WHERE observation_id IN ("
        "   SELECT id FROM balance_observation WHERE wallet_id = :wid"
        " )",
    )
    await _delete(
        "balance_observation",
        "DELETE FROM balance_observation WHERE wallet_id = :wid",
    )
    await _delete(
        "discovery_coverage",
        "DELETE FROM discovery_coverage WHERE wallet_id = :wid",
    )
    await _delete(
        "monitored_pair",
        "DELETE FROM monitored_pair WHERE wallet_id = :wid",
    )
    await _delete(
        "onchain_event",
        "DELETE FROM onchain_event WHERE wallet_id = :wid",
    )
    await _delete(
        "event_indexer_checkpoint",
        "DELETE FROM event_indexer_checkpoint WHERE wallet_id = :wid",
    )
    # history_point hangs off valuation_snapshot, so clear it for the snapshots
    # this wallet just emptied, then drop those snapshots.  Snapshots that still
    # carry lines from other wallets are kept, but their history_point's stored
    # total and counts still include the lines just deleted, so re-derive them
    # from what survives — otherwise the chart keeps charting a deleted wallet's
    # value forever, and GET /history's exclusion re-cut subtracts a sum of
    # surviving lines from a total those lines never added up to (AUD-454).
    if touched_snapshots:
        remat = await rematerialize_history_points(session, touched_snapshots)
        deleted["history_point"] = remat["deleted"]
        await _delete(
            "valuation_snapshot",
            "DELETE FROM valuation_snapshot"
            " WHERE id IN :snapshot_ids"
            "   AND NOT EXISTS ("
            "     SELECT 1 FROM valuation_line vl"
            "     WHERE vl.snapshot_id = valuation_snapshot.id"
            "   )",
        )
    else:
        deleted["history_point"] = 0
        deleted["valuation_snapshot"] = 0
    await _delete("wallet", "DELETE FROM wallet WHERE id = :wid")

    await session.flush()
    await session.commit()
    return deleted


async def list_wallets(session: AsyncSession) -> list[Wallet]:
    """Return all tracked wallets, ordered by creation time."""
    result = await session.execute(sa.select(Wallet).order_by(Wallet.created_at))
    wallets = list(result.scalars())
    await _attach_wallet_fields(session, wallets)
    return wallets


async def list_wallets_page(
    session: AsyncSession,
    *,
    cursor: uuid.UUID | None = None,
    limit: int = 20,
) -> tuple[list[Wallet], uuid.UUID | None]:
    """Return up to *limit* wallets, oldest first, with keyset pagination.

    Returns (wallets, next_cursor). Pass next_cursor back in as *cursor* to
    fetch the following page; None means the list is exhausted.
    """
    stmt = sa.select(Wallet).order_by(Wallet.created_at, Wallet.id).limit(limit + 1)
    if cursor is not None:
        cursor_wallet = await session.get(Wallet, cursor)
        if cursor_wallet is not None:
            stmt = stmt.where(
                sa.tuple_(Wallet.created_at, Wallet.id)
                > sa.tuple_(cursor_wallet.created_at, cursor_wallet.id)
            )

    result = await session.execute(stmt)
    wallets = list(result.scalars())
    has_more = len(wallets) > limit
    wallets = wallets[:limit]
    next_cursor = wallets[-1].id if has_more and wallets else None
    await _attach_wallet_fields(session, wallets)
    return wallets, next_cursor


async def get_wallet(
    session: AsyncSession,
    *,
    wallet_id: uuid.UUID,
) -> Wallet | None:
    """Fetch a wallet by ID or return None."""
    wallet = await session.get(Wallet, wallet_id)
    if wallet is not None:
        await _attach_wallet_fields(session, [wallet])
    return wallet


async def _get_or_raise(session: AsyncSession, wallet_id: uuid.UUID) -> Wallet:
    wallet = await session.get(Wallet, wallet_id)
    if wallet is None:
        raise WalletNotFoundError(str(wallet_id))
    await _attach_wallet_fields(session, [wallet])
    return wallet
