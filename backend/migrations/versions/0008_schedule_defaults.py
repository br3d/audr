"""Seed default schedule rows so polled job kinds stop hot-looping (AUD-356).

The ``schedule`` table had zero rows for every kind. ``claim_job`` treats a
missing row as "always due", so every poll of the worker's 5-second loop
re-claimed and immediately completed balance_scan/discovery/quote_refresh/
event_indexer/news_refresh with no cooldown at all — on staging this produced
~171k balance_scan runs and ~494k discovery runs in about two days, which rate
limited the configured RPC endpoint (HTTP 429) and, compounded by the
permanent-retry-block bug fixed alongside this migration, froze balance
tracking for two days.

Seeds one row per kind actually dispatched through the schedule-gated path in
``claim_job`` (the on-demand kinds — valuation, validate_rpc, validate_quotes —
never go through that gate, so they need no row). Intervals match the
frontend's existing ``SchedulesPage`` fallback display values for balances/
discovery/quotes, extended to the two kinds the UI doesn't yet expose
(event_indexer, news_refresh — the latter matching its existing in-process
15-minute self-throttle in ``audr.jobs.news``).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-01
"""

from __future__ import annotations

from alembic import op

revision = "0008"
down_revision: str | None = "0007"
branch_labels = None
depends_on = None

_DEFAULT_FRESHNESS_S = {
    "balance_scan": 300,  # 5 min
    "discovery": 3600,  # 1 hour
    "quote_refresh": 300,  # 5 min
    "event_indexer": 300,  # 5 min
    "news_refresh": 900,  # 15 min
}


def upgrade() -> None:
    for kind, freshness_s in _DEFAULT_FRESHNESS_S.items():
        op.execute(
            f"""
            INSERT INTO schedule (id, kind, enabled, freshness_s)
            VALUES (gen_random_uuid(), '{kind}', true, {freshness_s})
            ON CONFLICT (kind) DO NOTHING
            """
        )


def downgrade() -> None:
    kinds = ", ".join(f"'{kind}'" for kind in _DEFAULT_FRESHNESS_S)
    op.execute(f"DELETE FROM schedule WHERE kind IN ({kinds})")
