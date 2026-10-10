"""Encrypt wallet.address behind a unique HMAC blind index (AUD-490).

`wallet.address` was the last owner-identifying column still stored as
plaintext (AUD-389 item 3b; AUD-488 did the same for `wallet.label`). Unlike
the label, the address carries a `unique=True` constraint and is looked up
by equality elsewhere in the codebase, so plaintext storage cannot simply be
swapped for an opaque envelope: this migration adds `address_ciphertext` (an
AES-256-GCM envelope, AAD bound to the wallet id under the `wallet:address:`
prefix — distinct from `wallet:label:` so an envelope cannot be moved between
the two columns) and `address_bidx` (a deterministic HMAC-SHA256 of the
normalised lowercase address, keyed by a subkey *derived* from the master key
via HKDF rather than the raw master key itself, so the index key leaking does
not help decrypt envelopes). The unique constraint moves from `address` to
`address_bidx`. The plaintext `address` column is then dropped — the service
layer (`audr.wallets.service`) handles encrypt/decrypt from here on, and
`wallet.address` is no longer a real column, only a transient attribute the
service layer attaches after decrypting.

Existing rows need the master key to encrypt, exactly like 0021. On a fresh
install there are no wallet rows yet, so the key step is skipped entirely
when the table is empty. On an existing stand, wallet rows imply the master
key was already initialised by a prior deploy, so `ensure_master_key_sync`
just unwraps it.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import hashlib
import hmac

import sqlalchemy as sa
from alembic import op
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from audr.operations.crypto import encrypt
from audr.operations.init_key import ensure_master_key_sync

revision = "0022"
down_revision: str | None = "0021"
branch_labels = None
depends_on = None

# Mirrors audr.wallets.service's _ADDRESS_AAD_PREFIX / _BIDX_HKDF_INFO.
# Duplicated here (rather than imported) because migrations must stay
# runnable even if a later refactor changes those constants' call sites —
# the on-disk envelope format is a durable contract, not an implementation
# detail of the current service module.
_ADDRESS_AAD_PREFIX = "wallet:address:"
_BIDX_HKDF_INFO = b"audr:wallet:address_bidx:v1"


def _bidx(address: str, master_key: bytes) -> bytes:
    subkey = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=_BIDX_HKDF_INFO
    ).derive(master_key)
    return hmac.new(subkey, address.encode("utf-8"), hashlib.sha256).digest()


def upgrade() -> None:
    op.add_column("wallet", sa.Column("address_ciphertext", sa.LargeBinary(), nullable=True))
    op.add_column("wallet", sa.Column("address_bidx", sa.LargeBinary(), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, address FROM wallet")).fetchall()
    if rows:
        key = ensure_master_key_sync(conn)
        for row in rows:
            address = (row.address or "").lower()
            aad = f"{_ADDRESS_AAD_PREFIX}{row.id!s}".encode()
            conn.execute(
                sa.text(
                    "UPDATE wallet SET address_ciphertext = :ct, address_bidx = :bidx"
                    " WHERE id = :id"
                ),
                {
                    "ct": encrypt(address.encode("utf-8"), aad, key),
                    "bidx": _bidx(address, key),
                    "id": row.id,
                },
            )

    op.alter_column(
        "wallet", "address_ciphertext", existing_type=sa.LargeBinary(), nullable=False
    )
    op.alter_column("wallet", "address_bidx", existing_type=sa.LargeBinary(), nullable=False)
    op.drop_constraint("uq_wallet_address", "wallet", type_="unique")
    # The CHECK tying plaintext address to its lowercased form goes with the
    # column it constrains — Postgres drops it automatically, but naming it
    # here would be wrong since drop_column already removes it.
    op.drop_column("wallet", "address")
    op.create_unique_constraint("uq_wallet_address_bidx", "wallet", ["address_bidx"])


def downgrade() -> None:
    # Lossy, like 0021's label downgrade: the plaintext cannot be recovered
    # from ciphertext, so this restores the column shape with a placeholder
    # rather than the original addresses. The placeholder is derived from each
    # row's id (zero-padded hex) rather than a fixed value, since the target
    # column is unique and a fixed placeholder would collide across rows.
    op.drop_constraint("uq_wallet_address_bidx", "wallet", type_="unique")
    op.add_column("wallet", sa.Column("address", sa.Text(), nullable=True))
    op.execute(
        "UPDATE wallet SET address = '0x' || lpad(replace(id::text, '-', ''), 40, '0')"
    )
    op.alter_column("wallet", "address", existing_type=sa.Text(), nullable=False)
    op.create_unique_constraint("uq_wallet_address", "wallet", ["address"])
    op.drop_column("wallet", "address_ciphertext")
    op.drop_column("wallet", "address_bidx")
