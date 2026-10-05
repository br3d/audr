"""Index balance_observation by (asset_id, wallet_id, observed_at) (AUD-433).

`GET /assets?held=` (AUD-432a) needs, per asset, whether any wallet's latest
observation for it is nonzero. The existing `ix_balance_observation_wallet_asset`
index leads with `wallet_id`, which serves the opposite lookup direction
(portfolio/snapshot.py's per-(wallet, asset) current-holdings query) but
cannot be used to find "the latest row per wallet for this asset" without
scanning every wallet's full history. This index leads with `asset_id` so
that lookup is a single index range scan instead.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from alembic import op

revision = "0019"
down_revision: str | None = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_balance_observation_asset_wallet "
        "ON balance_observation (asset_id, wallet_id, observed_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_balance_observation_asset_wallet")
