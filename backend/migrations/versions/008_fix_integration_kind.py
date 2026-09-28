"""Fix integration kind constraint: allow 'rpc' instead of 'rpc_mainnet'.

The worker and purge code use kind='rpc'; migration 002 used 'rpc_mainnet' by
mistake.  This migration drops and recreates ck_integration_kind with the
correct allowed values, and renames any existing 'rpc_mainnet' rows to 'rpc'.

Revision ID: 008
Revises: 007
Create Date: 2026-09-28
"""

from __future__ import annotations

from alembic import op

revision = "008"
down_revision = "007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop both possible constraint names (migration 002 may have created either or both).
    op.execute("ALTER TABLE integration DROP CONSTRAINT IF EXISTS ck_integration_kind")
    op.execute("ALTER TABLE integration DROP CONSTRAINT IF EXISTS ck_integration_ck_integration_kind")
    op.execute("UPDATE integration SET kind = 'rpc' WHERE kind = 'rpc_mainnet'")
    op.execute(
        "ALTER TABLE integration ADD CONSTRAINT ck_integration_kind"
        " CHECK (kind IN ('rpc', 'coingecko'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE integration DROP CONSTRAINT IF EXISTS ck_integration_kind")
    op.execute("ALTER TABLE integration DROP CONSTRAINT IF EXISTS ck_integration_ck_integration_kind")
    op.execute("UPDATE integration SET kind = 'rpc_mainnet' WHERE kind = 'rpc'")
    op.execute(
        "ALTER TABLE integration ADD CONSTRAINT ck_integration_kind"
        " CHECK (kind IN ('rpc_mainnet', 'coingecko'))"
    )
