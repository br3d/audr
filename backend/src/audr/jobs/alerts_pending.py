"""Print pending critical news alerts as JSON, for the host dispatcher (ADR-007 phase 2).

    docker compose exec -T worker python -m audr.jobs.alerts_pending
    docker compose exec -T worker python -m audr.jobs.alerts_pending --limit 5

This is one half of the container->host bridge ADR-007 §1 describes: `news_alert`
is the source of truth, written inside the stack by `news_analyze`, but the thing
that can actually deliver a push — `scripts/notify.sh` — is a host script with
`curl`, `python3` and `secrets/` that the worker image deliberately does not
have. So the worker *reports* what is pending and `scripts/alert-dispatch.sh`
does the sending.

Pending means exactly what ADR-007 §1 says, and the partial index
`ix_news_alert_pending` is built for this predicate:

    dispatched_at IS NULL AND severity = 'critical'

`warning` is deliberately not here: it accumulates in the in-app list, and push
is reserved for `critical` so the channel stays worth reading (ADR-007 §2,
"политика ложных срабатываний").

OUTPUT IS A CONTRACT. A JSON array on stdout, one object per alert, and nothing
else — diagnostics go to stderr, because the caller parses stdout. Each object
carries everything the alert text needs, so the dispatcher never queries the
database itself and needs no schema knowledge.

A MISSING TABLE IS NOT AN ERROR. Before phase 1's migration 0033 reaches a
stand, this prints `[]` and exits 0. That is what lets the timer be installed
and *proven firing* ahead of the table existing, instead of the install being
the last unproven step on the critical path.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

# Bounded so one bad night cannot turn into a hundred board issues in a single
# tick. The cooldown dedupe in `news_analyze` (one alert per asset x category
# per 24h) already caps the realistic rate far below this; this is the backstop
# for the case where that assumption is wrong.
DEFAULT_LIMIT = 20

# PostgreSQL's SQLSTATE for "relation does not exist". Matched on the code
# rather than the message so it survives a locale or wording change.
_UNDEFINED_TABLE = "42P01"

_PENDING_SQL = sa.text(
    """
    SELECT na.id,
           na.category,
           na.severity,
           na.summary,
           na.classifier,
           na.created_at,
           a.symbol      AS asset_symbol,
           a.name        AS asset_name,
           n.title       AS news_title,
           n.url         AS news_url,
           n.news_site   AS news_site,
           n.published_at
    FROM news_alert na
    JOIN asset a ON a.id = na.asset_id
    JOIN asset_news n ON n.id = na.asset_news_id
    WHERE na.dispatched_at IS NULL
      AND na.severity = 'critical'
    ORDER BY na.created_at
    LIMIT :limit
    """
)


def build_alert_key(symbol: str, category: str) -> str:
    """The `--key` notify.sh dedupes an ongoing condition by (ADR-007 §1).

    One key per asset x category, so the wave of articles about one incident
    comments on the board issue that is already open instead of opening a
    second one. Lowercased and stripped of anything outside [a-z0-9-] because
    this ends up in an issue title and a state filename; a symbol like `$MEME`
    or one with a space would otherwise produce two keys for one condition
    depending on how it was written.
    """
    raw = f"news-{symbol}-{category}".lower()
    cleaned = "".join(ch if ch.isalnum() else "-" for ch in raw)
    # Collapse runs of separators left by the substitution above.
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    return cleaned.strip("-")


def row_to_payload(row: sa.Row[Any]) -> dict[str, Any]:
    """Shape one `news_alert` row into the dispatcher's JSON object."""
    symbol = str(row.asset_symbol)
    category = str(row.category)
    return {
        "id": str(row.id),
        "key": build_alert_key(symbol, category),
        "category": category,
        "severity": str(row.severity),
        "summary": str(row.summary),
        "classifier": str(row.classifier),
        "created_at": row.created_at.isoformat(),
        "asset_symbol": symbol,
        "asset_name": str(row.asset_name),
        "title": str(row.news_title),
        "url": str(row.news_url),
        "news_site": str(row.news_site),
        "published_at": row.published_at.isoformat() if row.published_at else None,
    }


async def fetch_pending(
    session: AsyncSession, *, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """Pending critical alerts, oldest first. `[]` when the table does not exist yet."""
    try:
        result = await session.execute(_PENDING_SQL, {"limit": limit})
    except ProgrammingError as exc:
        # UndefinedTable (42P01) only: any other programming error is a real bug
        # and must not be swallowed into an empty, successful-looking result.
        if getattr(exc.orig, "sqlstate", None) == _UNDEFINED_TABLE:
            print(
                "[alerts_pending] news_alert does not exist yet (migration 0033 not applied) "
                "— reporting nothing pending",
                file=sys.stderr,
            )
            return []
        raise
    return [row_to_payload(row) for row in result.fetchall()]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m audr.jobs.alerts_pending",
        description="Print pending critical news alerts as JSON (ADR-007 phase 2).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"maximum alerts to report in one tick (default {DEFAULT_LIMIT})",
    )
    return parser.parse_args(argv)


async def _main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.limit < 1:
        print("[alerts_pending] --limit must be >= 1", file=sys.stderr)
        return 2

    from audr.db import _get_session_factory

    async with _get_session_factory()() as session:
        alerts = await fetch_pending(session, limit=args.limit)

    # Compact and newline-terminated: this is read by a shell, not a human.
    print(json.dumps(alerts, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
