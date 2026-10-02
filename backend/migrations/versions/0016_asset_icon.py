"""Asset icon cache table (rotki-style, AUD-385).

Backend-resolved icon cache, mirroring rotki's `icons.py`: the worker
resolves an asset's icon once from a keyless upstream (Trust Wallet's GitHub
asset repo, falling back to CoinGecko's keyless contract lookup) and stores
the bytes here; `GET /assets/{id}/icon` serves straight from this table and
never re-hits the upstream per request. Stored in Postgres rather than on
disk because the app containers have no persistent volume — a DB-backed
cache survives redeploys with no compose plumbing.

`status='missing'` is the negative-cache entry (rotki's `failed_asset_ids`):
recorded when neither upstream has an icon for the asset, so the refresh job
does not refetch it on every run within the negative-cache TTL. A row here
is only ever written by the asset_icon_refresh job, never inline in a
request.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision: str | None = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_icon",
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("image", sa.LargeBinary(), nullable=True),
        sa.Column("source", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("asset_id", name="pk_asset_icon"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_asset_icon_asset", ondelete="CASCADE"
        ),
        sa.CheckConstraint("status IN ('ok', 'missing')", name="ck_asset_icon_status"),
    )


def downgrade() -> None:
    op.drop_table("asset_icon")
