"""Record the on-chain block timestamp for balance reads (AUD-72).

`balance_observation` only ever stored `block_number` (the height) and
`observed_at` (the wall-clock moment the RPC call was made) — never the
timestamp of the block itself. `GET /portfolio` needs that block timestamp
to report `balance_block_time` / per-holding `block_time` per the contract
(the internal release-1 HTTP API contract): "Balance freshness is
measured from the last successful verified block time ... `balance_observed_at`
is the separate read time." Those are two different clocks — the chain's and
the scanner's — and conflating them (as the previous null-stub implementation
effectively did) would misreport freshness after a slow or retried scan.

`valuation_line.block_time` is a denormalized copy taken at publish time, the
same way `block_number` already is, so a published snapshot stays
self-contained and immutable even if `balance_observation` rows it read are
later superseded by newer scans.

Both columns are nullable: existing rows predate this capture and have no
block timestamp to backfill (the chain block they reference may no longer be
in a readily queryable range on the owner's RPC provider).

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision = "0015"
down_revision: str | None = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE balance_observation ADD COLUMN block_time timestamptz")
    op.execute("ALTER TABLE valuation_line ADD COLUMN block_time timestamptz")


def downgrade() -> None:
    op.execute("ALTER TABLE valuation_line DROP COLUMN block_time")
    op.execute("ALTER TABLE balance_observation DROP COLUMN block_time")
