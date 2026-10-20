"""Read-only API tokens (AUD-629, Stage 1 of docs/llm-access.md).

Introduces two tables:

``api_token`` — one row per minted token.  The plaintext is shown to the
owner exactly once and never stored; only the SHA-256 hex digest lives here.
``scopes`` is a TEXT column reserved for future narrowing (currently always
the static string ``'read'``).  ``redact_addresses`` is an opt-in per-token
flag that causes wallet addresses to be replaced by label + truncated hash in
responses, making it safe to paste a token into a hosted assistant without
revealing raw addresses.

``token_attempt`` — one row per use attempt, scoped to a token_id.  The same
throttle pattern used for login attempts (``login_attempt`` / service.py):
max 1000 failures in a 15-minute window per token before the token is
temporarily locked.  Success rows are written on every valid request and drive
``last_used_at`` refresh.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0036"
down_revision: str | None = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_token",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=False, server_default="read"),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("redact_addresses", sa.Boolean(), nullable=False, server_default="false"),
        sa.PrimaryKeyConstraint("id", name="pk_api_token"),
        sa.UniqueConstraint("token_hash", name="uq_api_token_token_hash"),
    )

    op.create_table(
        "token_attempt",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_token_attempt"),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["api_token.id"],
            name="fk_token_attempt_token_id_api_token",
            ondelete="CASCADE",
        ),
    )

    op.create_index(
        "ix_token_attempt_token_id_attempted_at",
        "token_attempt",
        ["token_id", "attempted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_token_attempt_token_id_attempted_at", table_name="token_attempt")
    op.drop_table("token_attempt")
    op.drop_table("api_token")
