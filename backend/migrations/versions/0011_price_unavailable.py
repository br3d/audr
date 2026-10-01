"""Track provider-confirmed unpriceable assets, add 'gaps' snapshot quality (AUD-361).

Four dust/exotic holdings (an Augur outcome share, tickers that collide with
better-known coins) will never resolve against the keyless CoinMarketCap map
built in AUD-358. `quote_refresh` had no way to tell "the provider was asked
about this asset and does not know it" apart from "this asset has never been
asked about at all" — so `total_usd` stayed permanently null on a real wallet.

`asset.price_unavailable_since` records the former: set whenever
`quote_refresh` requests a held asset's price and the provider's response
doesn't include it, cleared the moment a later run does resolve it. A holding
whose asset has this set does not block `total_usd`; a holding that was
simply never asked about still does.

`valuation_snapshot.quality` gains `'gaps'` — all holdings are either priced
or confirmed-unavailable, none are genuinely unknown, so a total is
computable even though coverage is incomplete.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op

revision = "0011"
down_revision: str | None = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE asset ADD COLUMN price_unavailable_since timestamptz")
    op.execute("ALTER TABLE valuation_snapshot DROP CONSTRAINT ck_valuation_snapshot_ck_valuation_snapshot_quality")
    op.execute(
        "ALTER TABLE valuation_snapshot ADD CONSTRAINT ck_valuation_snapshot_ck_valuation_snapshot_quality"
        " CHECK (quality IN ('complete', 'gaps', 'partial', 'stale', 'unknown'))"
    )


def downgrade() -> None:
    # 'gaps' snapshots predate the new value from the caller's perspective;
    # fold them back into 'partial' (some holdings unpriced) before the
    # narrower constraint is reinstated.
    op.execute("UPDATE valuation_snapshot SET quality = 'partial' WHERE quality = 'gaps'")
    op.execute("ALTER TABLE valuation_snapshot DROP CONSTRAINT ck_valuation_snapshot_ck_valuation_snapshot_quality")
    op.execute(
        "ALTER TABLE valuation_snapshot ADD CONSTRAINT ck_valuation_snapshot_ck_valuation_snapshot_quality"
        " CHECK (quality IN ('complete', 'partial', 'stale', 'unknown'))"
    )
    op.execute("ALTER TABLE asset DROP COLUMN price_unavailable_since")
