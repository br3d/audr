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
    op.create_check_constraint(
        "ck_news_alert_dispatched_via",
        "news_alert",
        "dispatched_via IS NULL OR dispatched_via = ANY (ARRAY["
        + ", ".join(f"'{road}'::text" for road in _ROADS)
        + "])",
    )


def downgrade() -> None:
    op.drop_constraint("ck_news_alert_dispatched_via", "news_alert", type_="check")
    op.drop_column("news_alert", "dispatched_via")
