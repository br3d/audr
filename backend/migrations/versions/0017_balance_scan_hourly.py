"""Raise the balance_scan schedule interval to 1 hour (AUD-397).

0009 seeded ``balance_scan`` at 5 minutes, which is far more often than the
owner wants for a long-lived wallet set — it burns RPC quota continuously for
balances that barely move between scans. 0013 already raised ``discovery`` to
24h and ``quote_refresh`` to 1h for the same reason; this does the same for
balance scans, which is the default the owner asked for.

Only rows still sitting at the old 300s default are touched, so an owner who
has already customised the interval on the Schedules page keeps their value.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from alembic import op

revision = "0017"
down_revision: str | None = "0016"
branch_labels = None
depends_on = None

_OLD_S = 300
_NEW_S = 3600


def upgrade() -> None:
    op.execute(
        f"""
        UPDATE schedule SET freshness_s = {_NEW_S}
        WHERE kind = 'balance_scan' AND freshness_s = {_OLD_S}
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        UPDATE schedule SET freshness_s = {_OLD_S}
        WHERE kind = 'balance_scan' AND freshness_s = {_NEW_S}
        """
    )
