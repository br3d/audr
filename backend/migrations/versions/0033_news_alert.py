"""News alerts core: analyzed_at cursor + news_alert table (ADR-007 / AUD-583).

Phase 1 of ADR-007 (AUD-337, rev 2.1). `news_analyze` (audr.jobs.news_analyze)
walks `asset_news` ordered by `analyzed_at IS NULL, published_at` and stamps
`analyzed_at` as its cursor, same idiom as `news_feed.last_polled_at`. Alerts
it creates land in `news_alert` — the source-of-truth table phase 2's host
dispatcher (`alert-dispatch.sh`) will read from via `dispatched_at`.

DDL is ADR-007 §4 as specified, with two corrections called out on AUD-583:

1. The ADR's own rev 2.1 text was written against a tree still on migration
   0006, so it labelled this "migration 0007" — by the time this phase
   actually started the tree was on 0032, making this 0033 instead.
2. The `ck_integration_kind` change in the ADR's DDL block is intentionally
   dropped. This phase ships heuristic-only classification (`classifier`
   stays constrained to `'llm'|'heuristic'` so the column/constraint shape
   is already correct for phase 3), and `'anthropic'` is not added to
   `integration` until phase 3 brings the LLM consumer that would actually
   use it — no dead enum value sitting in the constraint meanwhile.

No `schedule` row is seeded for the new `news_analyze` job kind — same as
`asset_icon_refresh`, it relies on `jobs.store._DEFAULT_FRESHNESS_S`'s
in-process fallback rather than a migration-seeded row.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0033"
down_revision: str | None = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("asset_news", sa.Column("analyzed_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "news_alert",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_news_id", sa.Uuid(), nullable=False),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("classifier", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dispatch_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_news_alert"),
        sa.ForeignKeyConstraint(
            ["asset_news_id"],
            ["asset_news.id"],
            name="fk_news_alert_asset_news",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["asset_id"], ["asset.id"], name="fk_news_alert_asset"),
        sa.CheckConstraint(
            "category = ANY (ARRAY["
            "'hack_exploit'::text, 'depeg'::text, 'delisting'::text,"
            " 'regulatory'::text, 'market_move'::text, 'other'::text])",
            name="ck_news_alert_category",
        ),
        sa.CheckConstraint(
            "severity = ANY (ARRAY['warning'::text, 'critical'::text])",
            name="ck_news_alert_severity",
        ),
        sa.CheckConstraint(
            "classifier = ANY (ARRAY['llm'::text, 'heuristic'::text])",
            name="ck_news_alert_classifier",
        ),
    )
    op.create_index(
        "ix_news_alert_unread",
        "news_alert",
        [sa.text("created_at DESC")],
        postgresql_where=sa.text("read_at IS NULL"),
    )
    op.create_index(
        "ix_news_alert_pending",
        "news_alert",
        ["created_at"],
        postgresql_where=sa.text("dispatched_at IS NULL AND severity = 'critical'"),
    )


def downgrade() -> None:
    op.drop_index("ix_news_alert_pending", table_name="news_alert")
    op.drop_index("ix_news_alert_unread", table_name="news_alert")
    op.drop_table("news_alert")
    op.drop_column("asset_news", "analyzed_at")
