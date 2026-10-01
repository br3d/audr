"""Raise discovery and quote_refresh schedule intervals (AUD-366).

0009 seeded discovery at 1h and quote_refresh at 5min. Both burn through RPC
and quote-provider quota faster than the owner wants for a single long-lived
wallet set: ERC-20 token discovery doesn't need to re-scan hourly, and price
quotes refreshing every 5 minutes outruns what free-tier quote providers
allow. Raise discovery to 24h and quote_refresh to 1h.

Only rows still sitting at the old default are touched, so an owner who has
already customised a schedule via the Schedules page keeps their value.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from alembic import op

revision = "0013"
down_revision: str | None = "0012"
branch_labels = None
depends_on = None

_RAISED_INTERVALS_S = {
    "discovery": (3600, 86400),  # 1h -> 24h
    "quote_refresh": (300, 3600),  # 5min -> 1h
}


def upgrade() -> None:
    for kind, (old_s, new_s) in _RAISED_INTERVALS_S.items():
        op.execute(
            f"""
            UPDATE schedule SET freshness_s = {new_s}
            WHERE kind = '{kind}' AND freshness_s = {old_s}
            """
        )


def downgrade() -> None:
    for kind, (old_s, new_s) in _RAISED_INTERVALS_S.items():
        op.execute(
            f"""
            UPDATE schedule SET freshness_s = {old_s}
            WHERE kind = '{kind}' AND freshness_s = {new_s}
            """
        )
