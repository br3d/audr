"""Mapping a new symbol also resets the feed's poll due-time (AUD-573 follow-up).

0029 cleared `etag`/`last_modified` on a `news_feed_symbol` INSERT so the
next poll can't get 304'd before the new symbol is parsed. That alone isn't
enough: `list_due_feeds` only returns feeds whose `last_polled_at` is NULL
or older than the 15-minute cutoff, so a feed that happens to have been
polled recently sits un-due — with a clean etag — until its normal window
comes back around. Observed live on AUD-573's own fix: zeroing `etag` by
hand produced `feeds_polled=0` until `last_polled_at` was zeroed too.

Widening the trigger to also null `last_polled_at` makes "symbol mapped"
take effect on the very next `news_refresh` run instead of waiting out
whatever's left of the current poll window.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

from alembic import op

revision = "0030"
down_revision: str | None = "0029"
branch_labels = None
depends_on = None

_FUNCTION_NAME = "news_feed_symbol_invalidate_etag"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION_NAME}() RETURNS trigger AS $$
        BEGIN
            UPDATE news_feed
            SET etag = NULL, last_modified = NULL, last_polled_at = NULL
            WHERE id = NEW.feed_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """  # noqa: S608 -- the only interpolation is _FUNCTION_NAME, a module constant
    )


def downgrade() -> None:
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION {_FUNCTION_NAME}() RETURNS trigger AS $$
        BEGIN
            UPDATE news_feed
            SET etag = NULL, last_modified = NULL
            WHERE id = NEW.feed_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """  # noqa: S608 -- the only interpolation is _FUNCTION_NAME, a module constant
    )
