"""Record the outcome of a host dispatch attempt on `news_alert` (ADR-007 phase 2).

    docker compose exec -T worker python -m audr.jobs.alerts_mark <id> [<id>...]
    docker compose exec -T worker python -m audr.jobs.alerts_mark --via spool <id>
    docker compose exec -T worker python -m audr.jobs.alerts_mark --error "notify.sh exit 1" <id>

The other half of `audr.jobs.alerts_pending`. Success sets `dispatched_at` and
clears any previous `dispatch_error`; `--error` records why an attempt failed and
deliberately leaves `dispatched_at` NULL, so the next tick retries the alert.

WHAT `dispatched_at` MEANS (ADR-007 §1, fixed in rev 2.1): notify.sh *accepted*
the message — exit 0 — not that a human saw it. notify.sh owns delivery from
that point: it retries Telegram itself, and anything no sink took is spooled and
replayed by `scripts/drain-alert-spool.py`. Re-implementing retries on top of
that would mean two schedules fighting over one message.

WHICH IS WHY `--via` EXISTS (AUD-595). "Accepted" covers two very different
outcomes — a sink pushed it at a human, or no sink took it and it is parked in
the spool until an agent runs the drain — and until `dispatched_via` the table
could not tell them apart. `--via` records the road notify.sh reported in its
`--road-file` (`board`, `telegram`, `board+telegram`, `spool`), so
`dispatched_via = 'spool'` reads as "out of the queue, nobody pushed yet". It
stays optional: a dispatcher that does not pass it — an install directory that
predates this, a hand-run — still marks the row and records no road, which is
honest rather than invented.

AT-LEAST-ONCE IS ACCEPTED, not a bug to fix here: if the dispatcher dies between
notify.sh returning 0 and this marking the row, the next tick sends the alert
again. The `--key` dedupe on the board sink collapses that into a comment on the
issue already open, which is why the ADR takes this trade rather than paying for
a transactional outbox.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

# Truncated before it reaches the column: the value is for an operator reading
# the UI, and a stack trace from a failing sink would push the useful first line
# out of sight.
ERROR_MAX_CHARS = 500

# The roads notify.sh can report in its --road-file, and the only values
# `dispatched_via` accepts (ck_news_alert_dispatched_via, migration 0034).
# `none` and `dry-run` are deliberately absent: the dispatcher only marks a row
# after notify.sh returned 0 having sent or spooled it, so either of those
# arriving here means the caller is confused and should fail loudly.
DISPATCH_ROADS = ("board", "telegram", "board+telegram", "spool")

_MARK_DISPATCHED_SQL = sa.text(
    """
    UPDATE news_alert
    SET dispatched_at = now(),
        dispatched_via = :via,
        dispatch_error = NULL
    WHERE id = ANY(:ids ::uuid[])
      AND dispatched_at IS NULL
    """
)

# The same UPDATE without the column AUD-595 added. An image can reach a stand
# before `alembic upgrade head` has added `dispatched_via` — the inverse of the
# AUD-584 outage, where the image was the older half — and in that window the
# column-less UPDATE is the one that must still happen: failing it would leave
# the row pending and re-send the same alert on every tick until the migration
# lands. Recording no road is the correct degradation; losing the mark is not.
_MARK_DISPATCHED_NO_VIA_SQL = sa.text(
    """
    UPDATE news_alert
    SET dispatched_at = now(),
        dispatch_error = NULL
    WHERE id = ANY(:ids ::uuid[])
      AND dispatched_at IS NULL
    """
)

_UNDEFINED_COLUMN = "42703"

_MARK_ERROR_SQL = sa.text(
    """
    UPDATE news_alert
    SET dispatch_error = :error
    WHERE id = ANY(:ids ::uuid[])
      AND dispatched_at IS NULL
    """
)


def parse_ids(raw: Sequence[str]) -> list[uuid.UUID]:
    """Validate the ids the dispatcher passed on the command line.

    Strict on purpose: these arrive from a shell loop over JSON, and a mangled
    argument list must fail visibly rather than silently update nothing.
    """
    ids: list[uuid.UUID] = []
    for item in raw:
        ids.append(uuid.UUID(item))
    return ids


async def mark_dispatched(
    session: AsyncSession, ids: Sequence[uuid.UUID], via: str | None = None
) -> int:
    """Stamp `dispatched_at` (+ the road). Rows already dispatched are no-ops."""
    try:
        result = await session.execute(
            _MARK_DISPATCHED_SQL, {"ids": [str(i) for i in ids], "via": via}
        )
    except ProgrammingError as exc:
        # UndefinedColumn (42703) only: any other programming error is a real
        # bug and must not be papered over with a silent second UPDATE.
        if getattr(exc.orig, "sqlstate", None) != _UNDEFINED_COLUMN:
            raise
        print(
            "[alerts_mark] news_alert.dispatched_via does not exist yet "
            "(migration 0034 not applied) — marking dispatched without the road",
            file=sys.stderr,
        )
        await session.rollback()
        result = await session.execute(_MARK_DISPATCHED_NO_VIA_SQL, {"ids": [str(i) for i in ids]})
    await session.commit()
    return int(result.rowcount or 0)


async def mark_error(session: AsyncSession, ids: Sequence[uuid.UUID], error: str) -> int:
    """Record why dispatch failed, leaving the alert pending for the next tick."""
    result = await session.execute(
        _MARK_ERROR_SQL,
        {"ids": [str(i) for i in ids], "error": error[:ERROR_MAX_CHARS]},
    )
    await session.commit()
    return int(result.rowcount or 0)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m audr.jobs.alerts_mark",
        description="Mark news alerts dispatched, or record a dispatch error (ADR-007 phase 2).",
    )
    parser.add_argument(
        "--error",
        default=None,
        help="record this dispatch error instead of marking the alerts dispatched",
    )
    parser.add_argument(
        "--via",
        default=None,
        choices=DISPATCH_ROADS,
        help="the road notify.sh reported (its --road-file); 'spool' means nobody was pushed",
    )
    parser.add_argument("ids", nargs="+", help="news_alert ids (uuid)")
    return parser.parse_args(argv)


async def _main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        ids = parse_ids(args.ids)
    except ValueError as exc:
        print(f"[alerts_mark] not a uuid: {exc}", file=sys.stderr)
        return 2

    from audr.db import _get_session_factory

    async with _get_session_factory()() as session:
        if args.error is None:
            changed = await mark_dispatched(session, ids, args.via)
            via = f" via {args.via}" if args.via else ""
            print(f"[alerts_mark] dispatched {changed}/{len(ids)}{via}")
        else:
            changed = await mark_error(session, ids, args.error)
            print(f"[alerts_mark] recorded dispatch_error on {changed}/{len(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
