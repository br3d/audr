"""Asset exclusion flag and decimals override column (T058 / US2 / AUD-71).

Revision ID: 005
Revises: 004
Create Date: 2026-09-28
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # excluded = true hides the asset from portfolio valuation and API responses.
    op.add_column(
        "asset",
        sa.Column(
            "excluded",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    # decimals_override allows the owner to correct on-chain decimals for display.
    op.add_column(
        "asset",
        sa.Column("decimals_override", sa.SmallInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("asset", "decimals_override")
    op.drop_column("asset", "excluded")
