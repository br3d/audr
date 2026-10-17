"""news_alert.dispatched_via: which road an alert actually took (AUD-595).

`dispatched_at` has meant "notify.sh accepted it" since ADR-007 rev 2.1, and
notify.sh accepts an alert it could not deliver to anybody — it parks it in
`~/.local/state/audr-notify/undelivered.jsonl` and exits 0, because the spool is
what `scripts/drain-alert-spool.py` replays (AUD-591). So a row that was pushed
to a human and a row sitting on a disk until an agent happens to run the drain
were indistinguishable in this table, and the dispatch-lag watchdog (AUD-592)
could not see the difference either.

This column is that distinction, recorded by `audr.jobs.alerts_mark --via` from
the road notify.sh reports in its `--road-file`:

    board | telegram | board+telegram   a sink took it — a human was pushed at
    spool                               nobody was; the drain owns it now

`dispatched_at` still gets stamped on the spool road, deliberately: spool->drain
IS an accepted delivery path, so the row has left the dispatcher's queue and
must not be re-sent every tick. The truth about *which* path lives here instead,
and the staleness of the spool itself is watched by its own artefact — the age
of the oldest line in `undelivered.jsonl` (`check_alert_spool_age`), not by
rows staying pending. Making the row stay pending was the other option on
AUD-595 and was rejected: with the stand's board token answering 401 it would
have left every alert pending forever and made the new watchdog check fire
continuously, trading a silent gap for a muted channel.

NULLABLE, with no backfill. Rows dispatched before this migration were sent
under the old contract, where the road was genuinely not recorded; inventing one
for them would be worse than NULL, which reads exactly as "dispatched before
AUD-595, road unknown".

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision: str | None = "0033"
branch_labels = None
depends_on = None

# The roads notify.sh can report, plus the two it reports when nothing was sent
# at all (`none`, `dry-run`) — those are never written here by the dispatcher,
# which only marks a row after notify.sh returned 0, but the constraint stays
# aligned with the vocabulary rather than with one caller's current branches.
_ROADS = ("board", "telegram", "board+telegram", "spool")


def upgrade() -> None:
    op.add_column("news_alert", sa.Column("dispatched_via", sa.Text(), nullable=True))
    # BARE name, not the full one. `audr.models`' naming convention is
    # `ck_%(table_name)s_%(constraint_name)s` and alembic applies it to the
    # op-level helpers too, so "ck_news_alert_dispatched_via" here would create
    # `ck_news_alert_ck_news_alert_dispatched_via` — the same way the 0001
    # baseline ended up with `ck_asset_ck_asset_address_lower`.
    #
    # That doubling was cosmetic, NOT a broken rollback: the convention is
    # applied on `op.drop_constraint` too, so the original spelling of this
    # migration agreed with itself in both directions. What it does mean is
    # that this column's CHECK is named differently depending on *when* a
    # database was migrated, which is why `downgrade()` below looks the name
    # up instead of spelling it.
    op.create_check_constraint(
        "dispatched_via",
        "news_alert",
        "dispatched_via IS NULL OR dispatched_via = ANY (ARRAY["
        + ", ".join(f"'{road}'::text" for road in _ROADS)
        + "])",
    )


def downgrade() -> None:
    # Resolve the real name rather than spell it, because this column's CHECK
    # has two names in the wild and a literal is wrong on one of them:
    #
    #   migrated before a1a9bf4  ck_news_alert_ck_news_alert_dispatched_via
    #   migrated after  a1a9bf4  ck_news_alert_dispatched_via
    #
    # The stand is the first shape (0034 landed there under the original
    # spelling and an applied migration never re-runs), so pinning the second
    # spelling turned a rollback that worked into `UndefinedObject` on the one
    # database a rollback would ever be run against — and a downgrade is wanted
    # exactly when a deploy has migrated and then failed (deploy-runbook.md,
    # "A rollback must restore the schema"). Matching on the constraint
    # DEFINITION is what both shapes have in common.
    bind = op.get_bind()
    names = (
        bind.execute(
            sa.text(
                "SELECT c.conname FROM pg_constraint c"
                " JOIN pg_class t ON t.oid = c.conrelid"
                " WHERE t.relname = 'news_alert' AND c.contype = 'c'"
                "   AND pg_get_constraintdef(c.oid) LIKE '%dispatched_via%'"
            )
        )
        .scalars()
        .all()
    )
    for name in names:
        # Identifier from pg_constraint, not from input; quoted all the same.
        op.execute(f'ALTER TABLE news_alert DROP CONSTRAINT "{name}"')  # noqa: S608
    op.drop_column("news_alert", "dispatched_via")
