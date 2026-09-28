"""Owner, session, throttle and encrypted integration tables (T026 / US1).

Revision ID: 002
Revises: 001
Create Date: 2026-09-27
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: str | None = "001"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # owner — singleton row enforced by a partial unique index on singleton=TRUE
    # ------------------------------------------------------------------
    op.create_table(
        "owner",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("singleton", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("argon2_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_owner"),
    )
    # Enforce at most one row where singleton=TRUE.
    op.execute(
        "CREATE UNIQUE INDEX uq_owner_singleton ON owner (singleton) WHERE singleton = true"
    )

    # ------------------------------------------------------------------
    # session — server-side sessions with CSRF tokens
    # ------------------------------------------------------------------
    op.create_table(
        "session",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("csrf_token", sa.Text(), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_active_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_session"),
    )
    op.create_index("ix_session_expires_at", "session", ["expires_at"])

    # ------------------------------------------------------------------
    # login_attempt — append-only log of login attempts for throttle
    # ------------------------------------------------------------------
    op.create_table(
        "login_attempt",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "attempted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_login_attempt"),
    )
    op.create_index("ix_login_attempt_attempted_at", "login_attempt", ["attempted_at"])

    # ------------------------------------------------------------------
    # integration — encrypted RPC / API key settings (one row per kind)
    # ------------------------------------------------------------------
    op.create_table(
        "integration",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("encrypted_blob", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_integration"),
        sa.UniqueConstraint("kind", name="uq_integration_kind"),
        sa.CheckConstraint(
            "kind IN ('rpc_mainnet', 'coingecko')",
            name="ck_integration_kind",
        ),
    )


def downgrade() -> None:
    op.drop_table("integration")
    op.drop_table("login_attempt")
    op.drop_table("session")
    op.drop_table("owner")
