"""Per-wallet scope for on-demand balance_scan / discovery jobs (AUD-399/AUD-400).

The founder asked for "Refresh balances" and "Discover tokens" to move onto
each wallet card so a single address can be refreshed without burning RPC
calls on every tracked wallet. `job_run` has no way to carry a request
parameter today — `enqueue_job` only ever records `kind` — so there is
nowhere to stash which wallet a request should be scoped to.

`params` is a free-form jsonb blob (currently just `{"wallet_id": "..."}`
for scoped balance_scan/discovery requests) read back by the worker via
`get_job_params`. NULL means "no scope" and keeps today's
all-active-wallets behaviour for scheduled runs.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

from alembic import op

revision = "0018"
down_revision: str | None = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE job_run ADD COLUMN params jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE job_run DROP COLUMN params")
