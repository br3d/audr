"""LLM news-analysis gate: anthropic integration kind, news_suppressed,
asset_news.summary/importance, global gate setting (ADR-007 phase 3, AUD-658).

Phase 3 of ADR-007 (AUD-337) is the LLM consumer phase 2's migration (0033)
deliberately left out of `ck_integration_kind` — see that migration's
docstring. This migration brings it in.

Four independent pieces, all additive:

1. `ck_integration_kind` += `'anthropic'`, so the owner's Anthropic API key
   can be stored through the same encrypted `integration` row every other
   credential uses (`audr.settings.integrations`). No key is written here.
2. `asset_news` gains two nullable columns, `summary` and `importance`.
   Populated only for a row the LLM actually scored when the gate is on;
   every row ingested with the gate off (the default) leaves both NULL,
   same as a heuristic-only install today.
3. `news_suppressed`: a compact record of what the gate *dropped* —
   url/title/asset/score/reason/timestamp, no article body. This is what
   answers "why did news stop showing up for X" once the gate is live; it
   carries no weight itself and is pruned after 30 days by the job, same
   idiom as `asset_news`/`news_alert`'s 90-day prune (AUD-512/AUD-583), just
   a shorter window since this table exists for recent-memory debugging, not
   history.
4. `news_analysis_setting`: a one-row table (enforced by the boolean PK
   trick also used nowhere else in this schema yet, but the simplest correct
   way to guarantee exactly one row — `id boolean PRIMARY KEY DEFAULT true`
   plus `CHECK (id)` rejects a second row outright) holding the single
   global flag and the importance threshold the founder decided on (AUD-658
   wake comment, 2026-10-21 card `bea3dbdc`): one gate, not per-asset, off by
   default. Revision-checked the same way `integration`/`schedule` are.

Revision ID: 0043
Revises: 0042
Create Date: 2026-10-25
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0043"
down_revision: str | None = "0042"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    # `op.drop_constraint` re-applies the `ck_%(table_name)s_%(constraint_name)s`
    # naming convention to whatever name it is given (AUD-597's documented
    # trap, also hit by 0039/0041) — passing the already-prefixed name here
    # would double-prefix it (`ck_integration_ck_integration_kind`, which
    # does not exist). The bare token "kind" is what the convention turns
    # into the real, single-prefixed `ck_integration_kind` baseline (0001)
    # created via a literal `CREATE TABLE` dump, not an ORM construct, so
    # unlike the news_feed tier check there is only one name in the wild
    # here — no runtime pg_constraint lookup needed.
    op.drop_constraint("kind", "integration", type_="check")
    op.create_check_constraint(
        "kind",
        "integration",
        "kind = ANY (ARRAY['rpc'::text, 'coingecko'::text, 'anthropic'::text])",
    )

    op.add_column("asset_news", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("asset_news", sa.Column("importance", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(
        "importance_range",
        "asset_news",
        "importance IS NULL OR (importance >= 0 AND importance <= 100)",
    )

    op.create_table(
        "news_suppressed",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("asset_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_suppressed"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["asset.id"], name="fk_news_suppressed_asset", ondelete="CASCADE"
        ),
        sa.CheckConstraint("score >= 0 AND score <= 100", name="ck_news_suppressed_score"),
        # (asset_id, url) rather than (asset_id, source, external_id) like
        # asset_news: this table has no source/external_id (AUD-658 names only
        # url/title/asset_id/score/reason/ts) and url is the only stable key a
        # re-poll of the same feed/GDELT/HN item can dedup on — needed so a
        # re-poll of an already-suppressed article is recognised as "already
        # handled" without spending another LLM call on it.
        sa.UniqueConstraint("asset_id", "url", name="uq_news_suppressed_dedup"),
    )
    op.create_index(
        "ix_news_suppressed_created",
        "news_suppressed",
        ["created_at"],
    )

    op.create_table(
        "news_analysis_setting",
        sa.Column("id", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "importance_threshold", sa.SmallInteger(), server_default=sa.text("50"), nullable=False
        ),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_news_analysis_setting"),
        sa.CheckConstraint("id", name="ck_news_analysis_setting_singleton"),
        sa.CheckConstraint(
            "importance_threshold >= 0 AND importance_threshold <= 100",
            name="ck_news_analysis_setting_threshold",
        ),
    )
    op.execute("INSERT INTO news_analysis_setting (id) VALUES (true)")


def downgrade() -> None:
    op.drop_table("news_analysis_setting")
    op.drop_index("ix_news_suppressed_created", table_name="news_suppressed")
    op.drop_table("news_suppressed")
    op.drop_constraint("importance_range", "asset_news", type_="check")
    op.drop_column("asset_news", "importance")
    op.drop_column("asset_news", "summary")
    op.drop_constraint("kind", "integration", type_="check")
    op.create_check_constraint(
        "kind",
        "integration",
        "kind = ANY (ARRAY['rpc'::text, 'coingecko'::text])",
    )
