"""Repair corrupted string server defaults from the 0001 baseline dump (AUD-321).

The baseline was produced by a pg_dump round-trip that double-escaped three
string defaults, so the stored defaults were the literal strings ``'pending'``,
``''`` and ``'active'`` *including* the quote characters:

    quote_set.status DEFAULT '''pending'''::text
    wallet.label     DEFAULT ''''''::text
    wallet.status    DEFAULT '''active'''::text

Any INSERT that relied on those defaults would violate
``ck_quote_set_ck_quote_set_status`` / ``ck_wallet_ck_wallet_status`` (and would
store a quoted label). It was masked only because every code path supplies
these columns explicitly.

This migration fixes existing databases; ``0001_baseline.py`` was corrected in
the same change so fresh installs are clean too.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-30
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision: str | None = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE quote_set ALTER COLUMN status SET DEFAULT 'pending'")
    op.execute("ALTER TABLE wallet ALTER COLUMN label SET DEFAULT ''")
    op.execute("ALTER TABLE wallet ALTER COLUMN status SET DEFAULT 'active'")
    # wallet.label has no CHECK constraint, so a row created off the broken
    # default would carry the literal two-quote string. Normalise those.
    op.execute("UPDATE wallet SET label = '' WHERE label = ''''''")


def downgrade() -> None:
    # Intentionally a no-op: restoring the corrupted defaults would re-introduce
    # a constraint violation. Downgrading past this revision leaves the clean
    # defaults in place, which is safe for every earlier schema version.
    pass
