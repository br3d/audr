"""Allow 'approval' event_type on onchain_event — allowance monitoring (AUD-300).

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-29
"""

from __future__ import annotations

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

_OLD_CONSTRAINT = "event_type IN ('transfer_in', 'transfer_out')"
_NEW_CONSTRAINT = "event_type IN ('transfer_in', 'transfer_out', 'approval')"


def upgrade() -> None:
    op.drop_constraint("ck_onchain_event_type", "onchain_event", type_="check")
    op.create_check_constraint("ck_onchain_event_type", "onchain_event", _NEW_CONSTRAINT)


def downgrade() -> None:
    op.drop_constraint("ck_onchain_event_type", "onchain_event", type_="check")
    op.create_check_constraint("ck_onchain_event_type", "onchain_event", _OLD_CONSTRAINT)
