"""On-chain event log and indexer checkpoint tables (AUD-307).

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # onchain_event — append-only log of ERC-20 Transfer events
    # ------------------------------------------------------------------
    op.create_table(
        "onchain_event",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("tx_hash", sa.Text(), nullable=False),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        sa.Column("log_index", sa.Integer(), nullable=False),
        # 'transfer_in' | 'transfer_out'
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("token_address", sa.Text(), nullable=False),
        sa.Column("from_address", sa.Text(), nullable=False),
        sa.Column("to_address", sa.Text(), nullable=False),
        sa.Column("raw_amount", sa.Numeric(precision=78, scale=0), nullable=False),
        sa.Column(
            "indexed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_onchain_event"),
        sa.ForeignKeyConstraint(
            ["wallet_id"], ["wallet.id"], name="fk_onchain_event_wallet"
        ),
        sa.UniqueConstraint(
            "tx_hash", "log_index", name="uq_onchain_event_dedup"
        ),
        sa.CheckConstraint(
            "event_type IN ('transfer_in', 'transfer_out')",
            name="ck_onchain_event_type",
        ),
        sa.CheckConstraint(
            "token_address = lower(token_address)",
            name="ck_onchain_event_token_lower",
        ),
        sa.CheckConstraint(
            "from_address = lower(from_address)",
            name="ck_onchain_event_from_lower",
        ),
        sa.CheckConstraint(
            "to_address = lower(to_address)",
            name="ck_onchain_event_to_lower",
        ),
    )

    op.create_index(
        "ix_onchain_event_wallet_block",
        "onchain_event",
        ["wallet_id", sa.text("block_number DESC")],
    )

    # ------------------------------------------------------------------
    # event_indexer_checkpoint — last processed block per wallet
    # ------------------------------------------------------------------
    op.create_table(
        "event_indexer_checkpoint",
        sa.Column("wallet_id", sa.Uuid(), nullable=False),
        sa.Column("last_processed_block", sa.BigInteger(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("wallet_id", name="pk_event_indexer_checkpoint"),
        sa.ForeignKeyConstraint(
            ["wallet_id"],
            ["wallet.id"],
            name="fk_event_indexer_checkpoint_wallet",
        ),
    )


def downgrade() -> None:
    op.drop_table("event_indexer_checkpoint")
    op.drop_index("ix_onchain_event_wallet_block", table_name="onchain_event")
    op.drop_table("onchain_event")
