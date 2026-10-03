"""Deduplicate valuation-snapshot publication by exact input key (AUD-70).

publish_valuation_snapshot() inserted a new valuation_snapshot row on every
call with a fresh uuid4 id, and the only idempotency in the pipeline
(materialize_history_point) keys off that always-new snapshot_id. A retry of
the same observation set therefore published a new snapshot and a new
history point every time — exactly what "retry must not add history" rules
out.

`valuation_snapshot.input_key` is a sha256 of the sorted set of included
balance_observation ids plus the sorted set of quote_set ids the prices came
from. The same inputs (same holdings, same quote data) always hash to the
same key, so publish_valuation_snapshot can look the key up first and return
the existing snapshot instead of inserting a duplicate.

Existing rows predate this key and have no recorded inputs to recompute it
from; they are backfilled with their own id so the NOT NULL + UNIQUE
constraints hold without inventing a key that could coincidentally collide
with a future real one.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision: str | None = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE valuation_snapshot ADD COLUMN input_key text")
    op.execute("UPDATE valuation_snapshot SET input_key = id::text WHERE input_key IS NULL")
    op.execute("ALTER TABLE valuation_snapshot ALTER COLUMN input_key SET NOT NULL")
    op.execute(
        "ALTER TABLE valuation_snapshot ADD CONSTRAINT uq_valuation_snapshot_input_key"
        " UNIQUE (input_key)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE valuation_snapshot DROP CONSTRAINT uq_valuation_snapshot_input_key")
    op.execute("ALTER TABLE valuation_snapshot DROP COLUMN input_key")
