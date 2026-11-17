"""Server-side AI-assistant conversations (AUD-958).

`assistant_conversation` is one chat; `assistant_message` is one turn of it.
Until now the assistant (AUD-955) was stateless and the browser held the
conversation, so a reload, a closed tab mid-answer or another device lost it.

An assistant turn is written as a `pending` row *before* the LLM is called and
flipped to `done`/`error` afterwards, so a browser that disconnects mid-answer
still leaves a durable row. A `pending` row that outlives any possible request
is swept to `error` (`error_code = 'abandoned'`) by the API.

Messages cascade with their conversation; nothing else references either table.

Revision ID: 0044
Revises: 0043
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision: str | None = "0043"
branch_labels: None = None
depends_on: None = None


def upgrade() -> None:
    op.create_table(
        "assistant_conversation",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assistant_conversation"),
        sa.CheckConstraint(
            "mode IN ('portfolio', 'news', 'chat')", name="ck_assistant_conversation_mode"
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 120", name="ck_assistant_conversation_title"
        ),
    )
    op.create_index("ix_assistant_conversation_updated", "assistant_conversation", ["updated_at"])

    op.create_table(
        "assistant_message",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("context_as_of", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("elapsed_ms", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'done'"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_assistant_message"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["assistant_conversation.id"],
            name="fk_assistant_message_conversation",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_assistant_message_role"),
        sa.CheckConstraint(
            "status IN ('pending', 'done', 'error')", name="ck_assistant_message_status"
        ),
    )
    # Turns inserted in one transaction share now(); `seq` gives a total order.
    op.add_column(
        "assistant_message",
        sa.Column("seq", sa.BigInteger(), sa.Identity(always=False), nullable=False),
    )
    op.create_index(
        "ix_assistant_message_conversation", "assistant_message", ["conversation_id", "seq"]
    )


def downgrade() -> None:
    op.drop_table("assistant_message")
    op.drop_table("assistant_conversation")
