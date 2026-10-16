"""Record the outcome of a host dispatch attempt on `news_alert` (ADR-007 phase 2).

    docker compose exec -T worker python -m audr.jobs.alerts_mark <id> [<id>...]
    docker compose exec -T worker python -m audr.jobs.alerts_mark --error "notify.sh exit 1" <id>

The other half of `audr.jobs.alerts_pending`. Success sets `dispatched_at` and
clears any previous `dispatch_error`; `--error` records why an attempt failed and
deliberately leaves `dispatched_at` NULL, so the next tick retries the alert.

WHAT `dispatched_at` MEANS (ADR-007 §1, fixed in rev 2.1): notify.sh *accepted*
the message — exit 0 — not that a human saw it. notify.sh owns delivery from
that point: it retries Telegram itself, and anything no sink took is spooled and
replayed by `scripts/drain-alert-spool.py`. Re-implementing retries on top of
that would mean two schedules fighting over one message.

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
from sqlalchemy.ext.asyncio import AsyncSession

# Truncated before it reaches the column: the value is for an operator reading
# the UI, and a stack trace from a failing sink would push the useful first line
# out of sight.
ERROR_MAX_CHARS = 500

_MARK_DISPATCHED_SQL = sa.text(
    """
    UPDATE news_alert
    SET dispatched_at = now(),
        dispatch_error = NULL
    WHERE id = ANY(:ids ::uuid[])
      AND dispatched_at IS NULL
    """
)

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


async def mark_dispatched(session: AsyncSession, ids: Sequence[uuid.UUID]) -> int:
    """Stamp `dispatched_at`. Returns rows changed (already-dispatched rows are no-ops)."""
    result = await session.execute(_MARK_DISPATCHED_SQL, {"ids": [str(i) for i in ids]})
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
            changed = await mark_dispatched(session, ids)
            print(f"[alerts_mark] dispatched {changed}/{len(ids)}")
        else:
            changed = await mark_error(session, ids, args.error)
            print(f"[alerts_mark] recorded dispatch_error on {changed}/{len(ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
