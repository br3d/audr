"""Add 'empty' status to quote_set (AUD-273).

A successful provider call that returns no usable prices should be recorded
as 'empty' rather than 'complete' so that _get_latest_prices never shadows
an earlier set that carried real prices.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision: str | None = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quote_set DROP CONSTRAINT ck_quote_set_ck_quote_set_status")
    op.execute(
        "ALTER TABLE quote_set ADD CONSTRAINT ck_quote_set_ck_quote_set_status"
        " CHECK (status IN ('pending', 'complete', 'failed', 'empty'))"
    )


def downgrade() -> None:
    # Only rows that have status='empty' would violate the old constraint, so
    # convert them to 'failed' before reinstating it.
    op.execute("UPDATE quote_set SET status = 'failed' WHERE status = 'empty'")
    op.execute("ALTER TABLE quote_set DROP CONSTRAINT ck_quote_set_ck_quote_set_status")
    op.execute(
        "ALTER TABLE quote_set ADD CONSTRAINT ck_quote_set_ck_quote_set_status"
        " CHECK (status IN ('pending', 'complete', 'failed'))"
    )
