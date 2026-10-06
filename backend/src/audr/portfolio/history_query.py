"""History selection: 24h/7d/30d/all with <=2,000 points, gap markers, cursor
pagination (T070 / US3 / AUD-83).

query_history():
  Selects up to MAX_POINTS actual recorded history_point rows for the requested
  period.  No synthetic interpolation is performed — gaps in the timeline are
  surfaced as GapMarker entries between consecutive points.

  A GapMarker is inserted when the time delta between two adjacent points exceeds
  GAP_THRESHOLD_SECONDS (default: 2× the median inter-point interval, minimum 4h).

get_snapshot_detail():
  Returns the constituent valuation_lines for a specific snapshot, each annotated
  with the observation_id it was built from.

Pagination:
  cursor is the snapshot_id of the last item returned.  Pass it back as-is to
  get the next page.  When next_cursor is None the result set is exhausted.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

MAX_POINTS = 2_000
_MIN_GAP_SECONDS = 4 * 3600  # 4 hours
_DEFAULT_PERIOD_HOURS: dict[str, int | None] = {
    "24h": 24,
    "7d": 7 * 24,
    "30d": 30 * 24,
    "90d": 90 * 24,
    "1y": 365 * 24,
    "all": None,
}

# 90d/1y cover windows long enough that a dense (e.g. hourly) history would
# otherwise return thousands of raw points. When the window holds more than
# this many points we thin to one point per time bucket; short/sparse
# history (today's reality) stays untouched and matches 'all' exactly.
# 24h/7d/30d/all are unthinned — the frontend already depends on their
# point-for-point contract.
_THIN_TARGET_POINTS = 500
_THINNED_PERIODS = frozenset({"90d", "1y"})


@dataclass
class HistoryEntry:
    snapshot_id: uuid.UUID
    snapshotted_at: datetime
    total_value_usd: Decimal | None
    quality: str
    included_wallet_count: int
    included_asset_count: int
    has_gap: bool
    is_canonical: bool
    is_gap_marker: bool = False


@dataclass
class SnapshotLine:
    wallet_id: uuid.UUID
    asset_id: uuid.UUID
    token_address: str
    symbol: str
    raw_amount: str
    block_number: int
    price_usd: str | None
    value_usd: str | None
    observation_id: uuid.UUID | None


@dataclass
class HistoryPage:
    entries: list[HistoryEntry]
    next_cursor: uuid.UUID | None  # snapshot_id of the last entry; None when exhausted


@dataclass
class SnapshotDetail:
    snapshot_id: uuid.UUID
    snapshotted_at: datetime
    quality: str
    lines: list[SnapshotLine]


Period = Literal["24h", "7d", "30d", "90d", "1y", "all"]


async def query_history(
    session: AsyncSession,
    *,
    period: Period = "7d",
    cursor: uuid.UUID | None = None,
    limit: int = MAX_POINTS,
) -> HistoryPage:
    """Return up to `limit` history entries for the given period.

    Only canonical points are returned unless every point is non-canonical,
    in which case non-canonical points are returned (degraded mode).
    Gap markers are synthetic entries inserted between points that are farther
    apart than the computed gap threshold.
    """
    if limit < 1 or limit > MAX_POINTS:
        limit = MAX_POINTS

    since = _period_start(period)

    # Build query with cursor for keyset pagination.
    # We fetch limit+1 to detect whether a next page exists.
    params: dict[str, object] = {"limit": limit + 1}
    since_clause = ""
    if since is not None:
        since_clause = "AND snapshotted_at >= :since"
        params["since"] = since

    cursor_clause = ""
    if cursor is not None:
        cursor_clause = """
            AND snapshotted_at < (
                SELECT snapshotted_at FROM history_point WHERE snapshot_id = :cursor
            )
        """
        params["cursor"] = str(cursor)

    bucket_seconds = await _bucket_seconds(
        session, period=period, since_clause=since_clause, params=params
    )

    if bucket_seconds is None:
        rows = await session.execute(
            sa.text(
                f"""
                SELECT
                    hp.snapshot_id::text,
                    hp.snapshotted_at,
                    hp.total_value_usd::text,
                    hp.quality,
                    hp.included_wallet_count,
                    hp.included_asset_count,
                    hp.has_gap,
                    hp.is_canonical
                FROM history_point hp
                WHERE 1=1
                  {since_clause}
                  {cursor_clause}
                ORDER BY hp.snapshotted_at DESC
                LIMIT :limit
                """  # noqa: S608 -- since_clause/cursor_clause are fixed ":param" fragments; values are bound, never interpolated
            ),
            params,
        )
    else:
        # Thin to one (the most recent) point per bucket before paginating,
        # so a dense/long history doesn't return thousands of raw points.
        params["bucket_seconds"] = bucket_seconds
        rows = await session.execute(
            sa.text(
                f"""
                WITH candidates AS (
                    SELECT snapshot_id, snapshotted_at, total_value_usd, quality,
                           included_wallet_count, included_asset_count, has_gap,
                           is_canonical
                    FROM (
                        SELECT hp.*,
                               ROW_NUMBER() OVER (
                                   PARTITION BY floor(
                                       extract(epoch FROM hp.snapshotted_at) / :bucket_seconds
                                   )
                                   ORDER BY hp.snapshotted_at DESC
                               ) AS _bucket_rank
                        FROM history_point hp
                        WHERE 1=1
                          {since_clause}
                    ) hp
                    WHERE hp._bucket_rank = 1
                )
                SELECT
                    snapshot_id::text,
                    snapshotted_at,
                    total_value_usd::text,
                    quality,
                    included_wallet_count,
                    included_asset_count,
                    has_gap,
                    is_canonical
                FROM candidates
                WHERE 1=1
                  {cursor_clause}
                ORDER BY snapshotted_at DESC
                LIMIT :limit
                """  # noqa: S608 -- since_clause/cursor_clause are fixed ":param" fragments; values are bound, never interpolated
            ),
            params,
        )
    raw = rows.fetchall()

    # Determine if a next page exists.
    has_more = len(raw) > limit
    raw = raw[:limit]

    if not raw:
        return HistoryPage(entries=[], next_cursor=None)

    entries = [_row_to_entry(r) for r in raw]

    # Re-cut every point against the *current* exclusion set (AUD-448).
    entries = await _apply_exclusions(session, entries)

    # Inject gap markers between entries whose timestamps are far apart.
    gap_threshold = _compute_gap_threshold(entries)
    with_gaps = _inject_gaps(entries, gap_threshold)

    next_cursor = entries[-1].snapshot_id if has_more else None
    return HistoryPage(entries=with_gaps, next_cursor=next_cursor)


async def get_snapshot_detail(
    session: AsyncSession,
    *,
    snapshot_id: uuid.UUID,
) -> SnapshotDetail | None:
    """Return detailed snapshot lines with observation provenance."""
    snap_result = await session.execute(
        sa.text("SELECT snapshotted_at, quality FROM valuation_snapshot WHERE id = :sid"),
        {"sid": str(snapshot_id)},
    )
    snap_row = snap_result.first()
    if snap_row is None:
        return None

    lines_result = await session.execute(
        sa.text(
            """
            SELECT
                vl.wallet_id::text,
                vl.asset_id::text,
                a.token_address,
                a.symbol,
                vl.raw_amount::text,
                vl.block_number,
                vl.price_usd::text,
                vl.value_usd::text,
                vl.observation_id::text
            FROM valuation_line vl
            JOIN asset a ON a.id = vl.asset_id
            WHERE vl.snapshot_id = :sid
            ORDER BY vl.value_usd DESC NULLS LAST
            """
        ),
        {"sid": str(snapshot_id)},
    )

    lines = [
        SnapshotLine(
            wallet_id=uuid.UUID(row[0]),
            asset_id=uuid.UUID(row[1]),
            token_address=row[2],
            symbol=row[3],
            raw_amount=row[4],
            block_number=int(row[5]),
            price_usd=row[6],
            value_usd=row[7],
            observation_id=uuid.UUID(row[8]) if row[8] else None,
        )
        for row in lines_result
    ]

    return SnapshotDetail(
        snapshot_id=snapshot_id,
        snapshotted_at=snap_row[0],
        quality=snap_row[1],
        lines=lines,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _apply_exclusions(
    session: AsyncSession,
    entries: list[HistoryEntry],
) -> list[HistoryEntry]:
    """Re-cut each point's total against the assets excluded *right now*.

    history_point stores the gross total of its snapshot's lines. Excluding
    an asset must redraw the chart immediately rather than bend it only from
    the next snapshot onwards (AUD-448) — otherwise the curve and the
    headline total, which GET /portfolio already cuts at read time, disagree
    about the same instant, and the step in the line records when the owner
    pressed a button rather than anything the portfolio did.

    Points whose total is already unknown stay unknown. The subtraction is
    floored at zero: a point can only lose the value its own snapshot
    recorded for the excluded asset, so it cannot go negative, but rounding
    across historical prices should not be able to push it there either.

    Snapshots published before AUD-448 may predate the line for an asset
    excluded at the time; they simply have nothing to subtract, which is
    correct — their stored total never counted it.
    """
    if not entries:
        return entries

    result = await session.execute(
        sa.text(
            """
            SELECT
                vl.snapshot_id::text,
                COALESCE(SUM(vl.value_usd), 0)::text   AS excluded_value,
                COUNT(DISTINCT vl.asset_id)            AS excluded_assets
            FROM valuation_line vl
            JOIN asset a ON a.id = vl.asset_id
            WHERE vl.snapshot_id IN :snapshot_ids
              AND COALESCE(a.excluded, false)
            GROUP BY vl.snapshot_id
            """
        ).bindparams(sa.bindparam("snapshot_ids", expanding=True)),
        {"snapshot_ids": [e.snapshot_id for e in entries]},
    )
    adjustments = {row[0]: (Decimal(row[1]), int(row[2])) for row in result}
    if not adjustments:
        return entries

    adjusted: list[HistoryEntry] = []
    for entry in entries:
        adjustment = adjustments.get(str(entry.snapshot_id))
        if adjustment is None:
            adjusted.append(entry)
            continue
        excluded_value, excluded_assets = adjustment
        total = entry.total_value_usd
        if total is not None:
            total = max(Decimal(0), total - excluded_value)
        adjusted.append(
            HistoryEntry(
                snapshot_id=entry.snapshot_id,
                snapshotted_at=entry.snapshotted_at,
                total_value_usd=total,
                quality=entry.quality,
                included_wallet_count=entry.included_wallet_count,
                included_asset_count=max(0, entry.included_asset_count - excluded_assets),
                has_gap=entry.has_gap,
                is_canonical=entry.is_canonical,
                is_gap_marker=entry.is_gap_marker,
            )
        )
    return adjusted


def _period_start(period: Period) -> datetime | None:
    hours = _DEFAULT_PERIOD_HOURS.get(period)
    if hours is None:
        return None
    return datetime.now(tz=UTC) - timedelta(hours=hours)


async def _bucket_seconds(
    session: AsyncSession,
    *,
    period: Period,
    since_clause: str,
    params: dict[str, object],
) -> int | None:
    """Return the thinning bucket size in seconds for this request.

    Returns None when the period isn't subject to thinning, or when the
    window currently holds at most _THIN_TARGET_POINTS points (nothing to
    thin — short/sparse history is returned at full resolution).
    """
    if period not in _THINNED_PERIODS:
        return None

    count_params: dict[str, object] = {}
    if "since" in params:
        count_params["since"] = params["since"]
    # since_clause is a fixed ":param" fragment; the value is bound, never interpolated.
    sql = f"SELECT COUNT(*) FROM history_point hp WHERE 1=1 {since_clause}"  # noqa: S608 — since_clause is a fixed ":param" fragment; the value is bound, never interpolated
    raw_count = (await session.execute(sa.text(sql), count_params)).scalar_one()
    if raw_count <= _THIN_TARGET_POINTS:
        return None

    hours = _DEFAULT_PERIOD_HOURS[period]
    assert hours is not None  # noqa: S101 -- thinned periods always have a bounded window
    return max(1, (hours * 3600) // _THIN_TARGET_POINTS)


def _row_to_entry(row: object) -> HistoryEntry:
    total = Decimal(row[2]) if row[2] is not None else None  # type: ignore[index]
    return HistoryEntry(
        snapshot_id=uuid.UUID(str(row[0])),  # type: ignore[index]
        snapshotted_at=row[1],  # type: ignore[index]
        total_value_usd=total,
        quality=row[3],  # type: ignore[index]
        included_wallet_count=int(row[4]),  # type: ignore[index]
        included_asset_count=int(row[5]),  # type: ignore[index]
        has_gap=bool(row[6]),  # type: ignore[index]
        is_canonical=bool(row[7]),  # type: ignore[index]
    )


def _compute_gap_threshold(entries: list[HistoryEntry]) -> float:
    """Return the gap threshold in seconds.

    Uses 2× the median inter-point interval, with a floor of _MIN_GAP_SECONDS.
    Falls back to the floor when there are fewer than 2 entries, or when
    there is exactly one inter-point delta: with a single delta, that delta
    *is* the median, so `median * 2` would always exceed it and no gap
    could ever be detected regardless of how large the delta is. In that
    case the delta has nothing to be "distant" relative to, so fall back to
    the absolute floor instead of deriving a threshold from itself.
    """
    if len(entries) < 2:
        return float(_MIN_GAP_SECONDS)

    deltas = sorted(
        abs((entries[i].snapshotted_at - entries[i + 1].snapshotted_at).total_seconds())
        for i in range(len(entries) - 1)
    )
    if len(deltas) == 1:
        return float(_MIN_GAP_SECONDS)
    median = deltas[len(deltas) // 2]
    return max(float(_MIN_GAP_SECONDS), median * 2.0)


def _inject_gaps(
    entries: list[HistoryEntry],
    gap_threshold: float,
) -> list[HistoryEntry]:
    """Insert gap-marker entries between temporally distant consecutive points."""
    if len(entries) < 2:
        return list(entries)

    result: list[HistoryEntry] = [entries[0]]
    for i in range(1, len(entries)):
        delta = abs((entries[i - 1].snapshotted_at - entries[i].snapshotted_at).total_seconds())
        if delta > gap_threshold:
            # Synthetic gap marker between entries[i-1] and entries[i].
            midpoint = entries[i - 1].snapshotted_at - timedelta(seconds=delta / 2)
            result.append(
                HistoryEntry(
                    snapshot_id=entries[i - 1].snapshot_id,
                    snapshotted_at=midpoint,
                    total_value_usd=None,
                    quality="unknown",
                    included_wallet_count=0,
                    included_asset_count=0,
                    has_gap=True,
                    is_canonical=True,
                    is_gap_marker=True,
                )
            )
        result.append(entries[i])
    return result
