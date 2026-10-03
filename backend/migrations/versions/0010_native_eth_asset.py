"""Seed the native ETH asset and repair the UNKNOWN placeholder (AUD-360).

Two incompatible native-ETH sentinels coexisted: the jobs and price providers
wrote ``0xeeee…eeee`` while the ``/portfolio`` and ``/assets`` readers compared
against ``0x0000…0000``. Because the sentinels never matched, native holdings
were not flagged ``is_native`` and ``_ensure_asset`` minted a placeholder
``UNKNOWN / Unknown Token / source=manual`` row for ``0xeeee…eeee`` — which is
exactly the row sitting in the deployed database and the reason the largest
position in the portfolio renders as ``UNKNOWN``.

``0xeeee…eeee`` is the surviving sentinel, so this migration:

1. Repoints any rows that reference a legacy ``0x0000…0000`` asset onto the
   canonical asset, then drops the legacy row.
2. Upserts the canonical row with real ETH identity, overwriting the
   placeholder metadata in place so the asset id — and every
   ``balance_observation`` / ``valuation_line`` already pointing at it — stays
   valid.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision: str | None = "0009"
branch_labels = None
depends_on = None

NATIVE = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
LEGACY = "0x0000000000000000000000000000000000000000"

# Every table with a FK onto asset.id, so a legacy-row merge leaves no
# dangling reference. Enumerated from the live schema rather than guessed; a
# table added later and missed here makes the DELETE below fail loudly on the
# FK, which is the intended outcome — not a silent orphan.
_ASSET_REFERENCES = (
    "asset_metadata_revision",
    "asset_news",
    "balance_observation",
    "monitored_pair",
    "quote_observation",
    "valuation_line",
)


def _table_exists(conn: sa.engine.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            sa.text("SELECT to_regclass(:qualified) IS NOT NULL"),
            {"qualified": f"public.{table}"},
        ).scalar()
    )


def upgrade() -> None:
    conn = op.get_bind()

    canonical_id = conn.execute(
        sa.text("SELECT id FROM asset WHERE token_address = :a"), {"a": NATIVE}
    ).scalar()
    legacy_id = conn.execute(
        sa.text("SELECT id FROM asset WHERE token_address = :a"), {"a": LEGACY}
    ).scalar()

    if legacy_id is not None and canonical_id is None:
        # Only the legacy row exists: promote it so its FK references survive.
        conn.execute(
            sa.text("UPDATE asset SET token_address = :new WHERE id = :id"),
            {"new": NATIVE, "id": legacy_id},
        )
        canonical_id = legacy_id
        legacy_id = None

    if canonical_id is None:
        canonical_id = uuid.uuid4()
        conn.execute(
            sa.text(
                """
                INSERT INTO asset
                  (id, token_address, symbol, name, decimals, source, excluded)
                VALUES
                  (:id, :addr, 'ETH', 'Ethereum', 18, 'catalog', false)
                """
            ),
            {"id": str(canonical_id), "addr": NATIVE},
        )
    else:
        # Overwrite placeholder metadata in place; keep the id so existing
        # observations and valuation lines keep resolving.
        conn.execute(
            sa.text(
                """
                UPDATE asset
                   SET symbol = 'ETH',
                       name = 'Ethereum',
                       decimals = 18,
                       source = 'catalog',
                       decimals_override = NULL,
                       updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": canonical_id},
        )

    if legacy_id is not None:
        # Both rows existed: move every reference onto the canonical asset.
        for table in _ASSET_REFERENCES:
            if _table_exists(conn, table):
                conn.execute(
                    sa.text(
                        # table is from the hardcoded _ASSET_REFERENCES tuple above, not user input.
                        f"UPDATE {table} SET asset_id = :new WHERE asset_id = :old"  # noqa: S608 — table is from the hardcoded _ASSET_REFERENCES tuple above, not user input
                    ),
                    {"new": canonical_id, "old": legacy_id},
                )
        conn.execute(sa.text("DELETE FROM asset WHERE id = :id"), {"id": legacy_id})


def downgrade() -> None:
    # The pre-AUD-360 state is a corrupted placeholder; restoring it would
    # reintroduce the user-visible UNKNOWN holding. Only the metadata is
    # reverted, and the row is kept so FK references stay intact.
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE asset
               SET symbol = 'UNKNOWN',
                   name = 'Unknown Token',
                   source = 'manual',
                   updated_at = now()
             WHERE token_address = :addr
            """
        ),
        {"addr": NATIVE},
    )
