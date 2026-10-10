"""Encrypt wallet.label with the existing AES-256-GCM envelope (AUD-488).

`wallet.label` was the last free-text, owner-written column stored as
plaintext that is only ever read whole — never filtered, sorted or joined on
— so it is a candidate for the application-level envelope that already covers
provider credentials (`integration.encrypted_blob`, `crypto.py`). This
migration adds `label_ciphertext` (bytea), encrypts whatever plaintext labels
already exist under the master key, and drops the old `label` column. The
service layer (`audr.wallets.service`) handles encrypt/decrypt from here on;
`wallet.label` is no longer a real column, only a transient attribute the
service layer attaches after decrypting.

`wallet.address` is explicitly out of scope (tracked separately as item 3b —
it carries the unique constraint, which would have to move to a blind index).

Existing rows need the master key to encrypt, which lives in `key_state` and
requires SECRET_KEY. On a fresh install there are no wallet rows yet (this
migration runs before `audr.operations.init_key` ever does, per the `migrate`
container's `alembic upgrade head && python -m audr.operations.init_key`), so
the key step is skipped entirely when the table is empty. On an existing
stand, wallet rows imply the master key was already initialised by a prior
deploy (provider credentials were already in use), so
`ensure_master_key_sync` just unwraps it.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from audr.operations.crypto import encrypt
from audr.operations.init_key import ensure_master_key_sync

revision = "0021"
down_revision: str | None = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("wallet", sa.Column("label_ciphertext", sa.LargeBinary(), nullable=True))

    conn = op.get_bind()
    rows = conn.execute(sa.text("SELECT id, label FROM wallet")).fetchall()
    if rows:
        key = ensure_master_key_sync(conn)
        for row in rows:
            aad = f"wallet:label:{row.id!s}".encode()
            blob = encrypt((row.label or "").encode("utf-8"), aad, key)
            conn.execute(
                sa.text("UPDATE wallet SET label_ciphertext = :blob WHERE id = :id"),
                {"blob": blob, "id": row.id},
            )

    op.alter_column(
        "wallet",
        "label_ciphertext",
        existing_type=sa.LargeBinary(),
        nullable=False,
    )
    op.drop_column("wallet", "label")


def downgrade() -> None:
    # Lossy, like 0002's cron_expr downgrade: the plaintext cannot be recovered
    # from ciphertext, so this restores the column shape with an empty default
    # rather than the original values.
    op.add_column("wallet", sa.Column("label", sa.Text(), nullable=False, server_default=""))
    op.drop_column("wallet", "label_ciphertext")
