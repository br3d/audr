"""Mapping a new symbol to a feed invalidates its conditional GET (AUD-573).

AUD-571 made `news_feed` → asset many-to-many via `news_feed_symbol`, but
`_poll_feed` still sends `If-None-Match`/`If-Modified-Since` from
`news_feed.etag`/`last_modified` and returns on a 304 *before*
`_targets_for_feed()` ever runs. `project:ethereum-blog` and
`release:go-ethereum` (migration 0028) already had a validator from polling
WETH — mapping ETH to them did not change their URL or body, so upstream
kept returning 304 and ETH never saw the articles already sitting in
`asset_news` under WETH.

Fix chosen here is (a) from AUD-573's write-up, not (b) a registry-write
helper: `news_feed_symbol`'s main write path is a manual `INSERT` (AUD-568
part B is exactly founder-selected symbols going in that way), and a
helper only enforces the invalidation on call sites that remember to use
it. A trigger catches every writer, including `psql`, with no discipline
required. The trade-off is a second 304 only for the feed's *next* poll
after any mapping edit (even one that didn't actually need a re-fetch,
e.g. re-adding a symbol that was removed and re-added unchanged) — cheap
next to a feed silently missing a newly-mapped asset until the body
happens to change on its own.

Scoped to INSERT only: removing a symbol from a feed does not need the
next poll to see any new content, so there's nothing for a DELETE-triggered
invalidation to buy.

Revision ID: 0029
Revises: 0028
"""

from __future__ import annotations

from alembic import op

revision = "0029"
down_revision: str | None = "0028"
branch_labels = None
depends_on = None

_FUNCTION_NAME = "news_feed_symbol_invalidate_etag"
_TRIGGER_NAME = "trg_news_feed_symbol_invalidate_etag"


def upgrade() -> None:
    op.execute(
        f"""
        CREATE FUNCTION {_FUNCTION_NAME}() RETURNS trigger AS $$
        BEGIN
            UPDATE news_feed
            SET etag = NULL, last_modified = NULL
            WHERE id = NEW.feed_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """  # noqa: S608 -- the only interpolation is _FUNCTION_NAME, a module constant
    )
    op.execute(
        f"""
        CREATE TRIGGER {_TRIGGER_NAME}
        AFTER INSERT ON news_feed_symbol
        FOR EACH ROW
        EXECUTE FUNCTION {_FUNCTION_NAME}();
        """
    )


def downgrade() -> None:
    op.execute(f"DROP TRIGGER {_TRIGGER_NAME} ON news_feed_symbol")
    op.execute(f"DROP FUNCTION {_FUNCTION_NAME}()")
