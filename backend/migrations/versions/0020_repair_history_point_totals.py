"""Re-derive drifted history_point aggregates from their lines (AUD-454).

history_point.total_value_usd and the two counts are a denormalised aggregate of
the snapshot's valuation_lines, written once at publication. Two paths deleted
lines from already published snapshots without touching it:

- `delete_wallet` removed the wallet's valuation_lines and dropped only the
  history_points whose snapshot it emptied. A snapshot still holding another
  wallet's lines kept a total that counted the deleted wallet, so the chart went
  on charting an address the owner had removed.
- `purge_provider('coingecko')` deleted every priced line, leaving the
  history_points of snapshots with an unpriced survivor totalling prices that
  were gone.

Both are fixed at the source; this repairs the rows they already wrote. The
visible symptom is AUD-454: GET /history re-cuts a point by subtracting the sum
of its *surviving* excluded lines, so where the stored total was not built from
the surviving lines, excluding an asset moved the curve by the wrong amount or
not at all.

total_value_usd becomes NULL when no surviving line carries a value — the
point's value is unknown, not zero. Points whose snapshot has no lines left are
deleted outright.

Irreversible: the pre-repair aggregates described rows that no longer exist, so
there is nothing to restore them from. downgrade() is a no-op.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from alembic import op

revision = "0020"
down_revision: str | None = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DELETE FROM history_point hp
        WHERE NOT EXISTS (
          SELECT 1 FROM valuation_line vl WHERE vl.snapshot_id = hp.snapshot_id
        )
        """
    )
    op.execute(
        """
        UPDATE history_point hp
        SET total_value_usd = agg.total_value_usd,
            included_wallet_count = agg.wallet_count,
            included_asset_count = agg.asset_count
        FROM (
            SELECT vl.snapshot_id,
                   SUM(vl.value_usd)            AS total_value_usd,
                   COUNT(DISTINCT vl.wallet_id) AS wallet_count,
                   COUNT(DISTINCT vl.asset_id)  AS asset_count
            FROM valuation_line vl
            GROUP BY vl.snapshot_id
        ) agg
        WHERE hp.snapshot_id = agg.snapshot_id
          AND (
            hp.total_value_usd IS DISTINCT FROM agg.total_value_usd
            OR hp.included_wallet_count IS DISTINCT FROM agg.wallet_count
            OR hp.included_asset_count IS DISTINCT FROM agg.asset_count
          )
        """
    )


def downgrade() -> None:
    """No-op: the drifted aggregates cannot be reconstructed."""
