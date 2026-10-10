"""Stop storing the owner's own address in onchain_event (AUD-389 item 3c).

`onchain_event.from_address` / `to_address` are plain `text`, and
`_insert_event` only ever writes a row when one side of the event *is* the
tracked wallet: `transfer_out` requires `from_address == wallet`,
`transfer_in` requires `to_address == wallet`, `approval` requires the owner
topic to match. So every indexed row carried the owner's own address on disk
in the clear, even though `wallet_id` + `event_type` already determine which
side the owner was on — AUD-490 encrypted `wallet.address` but never touched
this table (see docs/verification-history.md §4).

The owner side is redundant, so this migration drops it rather than
encrypting it: a single `counterparty_address` column replaces both,
backfilled from whichever side `event_type` says is *not* the owner
(`transfer_in` -> old `from_address`; `transfer_out`/`approval` -> old
`to_address`). A third-party address is not owner-identifying and stays
plaintext — it is what `GET /events` and `GET /allowances` need to display
and filter on.

Self-transfer / self-approval rows (`from_address == to_address == wallet`)
would otherwise leak the owner's address right back into
`counterparty_address` — the backfill stores NULL for those instead.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023"
down_revision: str | None = "0022"
branch_labels = None
depends_on = None

_PLACEHOLDER = "('0x' || lpad(replace(wallet_id::text, '-', ''), 40, '0'))"


def upgrade() -> None:
    op.add_column("onchain_event", sa.Column("counterparty_address", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE onchain_event
        SET counterparty_address = CASE
            WHEN from_address = to_address THEN NULL
            WHEN event_type = 'transfer_in' THEN from_address
            ELSE to_address
        END
        """
    )

    op.drop_constraint("ck_onchain_event_from_lower", "onchain_event", type_="check")
    op.drop_constraint("ck_onchain_event_to_lower", "onchain_event", type_="check")
    op.drop_column("onchain_event", "from_address")
    op.drop_column("onchain_event", "to_address")

    op.create_check_constraint(
        "ck_onchain_event_counterparty_lower",
        "onchain_event",
        "counterparty_address = lower(counterparty_address)",
    )


def downgrade() -> None:
    # Lossy, like 0021/0022's downgrades: the owner side was never stored in
    # counterparty_address (that is the point of this migration), so it
    # cannot be recovered here — restoring it would mean decrypting
    # wallet.address inside a migration, which is the thing 0022 moved away
    # from. Both the owner side and any self-transfer/self-approval row get
    # the same id-derived placeholder 0022's downgrade uses, rather than a
    # fixed value, since nothing here guarantees distinctness across rows.
    op.add_column("onchain_event", sa.Column("from_address", sa.Text(), nullable=True))
    op.add_column("onchain_event", sa.Column("to_address", sa.Text(), nullable=True))

    op.execute(
        f"""
        UPDATE onchain_event SET
            from_address = CASE
                WHEN event_type = 'transfer_in' THEN COALESCE(counterparty_address, {_PLACEHOLDER})
                ELSE {_PLACEHOLDER}
            END,
            to_address = CASE
                WHEN event_type = 'transfer_in' THEN {_PLACEHOLDER}
                ELSE COALESCE(counterparty_address, {_PLACEHOLDER})
            END
        """
    )

    op.alter_column("onchain_event", "from_address", existing_type=sa.Text(), nullable=False)
    op.alter_column("onchain_event", "to_address", existing_type=sa.Text(), nullable=False)
    op.create_check_constraint(
        "ck_onchain_event_from_lower", "onchain_event", "from_address = lower(from_address)"
    )
    op.create_check_constraint(
        "ck_onchain_event_to_lower", "onchain_event", "to_address = lower(to_address)"
    )
    op.drop_constraint("ck_onchain_event_counterparty_lower", "onchain_event", type_="check")
    op.drop_column("onchain_event", "counterparty_address")
