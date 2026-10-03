"""Allow 'gaps' quality on history_point too (AUD-361 follow-up).

0011 widened valuation_snapshot.quality to accept 'gaps' but left the
identical check constraint on history_point untouched. materialize_history_point()
already treats 'gaps' as a has_gap case, so every valuation run that actually
produces a 'gaps' snapshot crashed on the history_point insert and rolled back
the whole transaction before the snapshot could be published — the same
total_usd-stuck-null symptom 0011 was meant to fix, just moved one table over.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op

revision = "0012"
down_revision: str | None = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE history_point DROP CONSTRAINT ck_history_point_ck_history_point_quality"
    )
    op.execute(
        "ALTER TABLE history_point ADD CONSTRAINT ck_history_point_ck_history_point_quality"
        " CHECK (quality IN ('complete', 'gaps', 'partial', 'stale', 'unknown'))"
    )


def downgrade() -> None:
    op.execute("UPDATE history_point SET quality = 'partial' WHERE quality = 'gaps'")
    op.execute(
        "ALTER TABLE history_point DROP CONSTRAINT ck_history_point_ck_history_point_quality"
    )
    op.execute(
        "ALTER TABLE history_point ADD CONSTRAINT ck_history_point_ck_history_point_quality"
        " CHECK (quality IN ('complete', 'partial', 'stale', 'unknown'))"
    )
