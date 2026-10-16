"""The worker's two dispatch CLI points (AUD-584, ADR-007 §1 phase 2).

`audr.jobs.alerts_pending` and `audr.jobs.alerts_mark` are the container half of
the bridge: the host dispatcher cannot reach the database and the worker cannot
reach notify.sh, so these two are the whole interface between them. What is
tested here is the part of that interface that is not SQL — the alert key, the
JSON shape the dispatcher parses, the uuid validation that stands between a
mangled shell loop and an UPDATE, and the deliberate "table does not exist yet
is not an error" path that lets the timer be installed before phase 1's
migration reaches a stand.

The SQL itself is covered where it can be covered honestly — against a real
database, once there are rows to read.
"""

from __future__ import annotations

import json
import types
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import ProgrammingError

from audr.jobs import alerts_mark, alerts_pending

pytestmark = pytest.mark.unit


def _row(**overrides: object) -> types.SimpleNamespace:
    base = {
        "id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "category": "hack_exploit",
        "severity": "critical",
        "summary": "Bridge exploit drains 40M",
        "classifier": "heuristic",
        "created_at": datetime(2026, 10, 16, 10, 0, tzinfo=UTC),
        "asset_symbol": "ETH",
        "asset_name": "Ether",
        "news_title": "Exploit hits bridge",
        "news_url": "https://example.test/a",
        "news_site": "example.test",
        "published_at": datetime(2026, 10, 16, 9, 55, tzinfo=UTC),
    }
    base.update(overrides)
    return types.SimpleNamespace(**base)


class _StubSession:
    """Minimal async session: either raises on execute, or returns rows."""

    def __init__(self, *, rows: list[object] | None = None, error: Exception | None = None) -> None:
        self._rows = rows or []
        self._error = error
        self.params: dict[str, object] | None = None

    async def execute(self, _statement: object, params: dict[str, object] | None = None) -> object:
        self.params = params
        if self._error is not None:
            raise self._error
        rows = self._rows
        return types.SimpleNamespace(fetchall=lambda: rows, rowcount=len(rows))

    async def commit(self) -> None:
        return None


def _programming_error(sqlstate: str) -> ProgrammingError:
    orig = types.SimpleNamespace(sqlstate=sqlstate)
    return ProgrammingError("SELECT ...", {}, orig)  # type: ignore[arg-type]


# --------------------------------------------------------------- alert key


def test_key_is_one_per_asset_and_category() -> None:
    # This string is what ties every message about one incident to a single
    # board issue; two spellings of it would mean two issues for one condition.
    assert alerts_pending.build_alert_key("ETH", "hack_exploit") == "news-eth-hack-exploit"


@pytest.mark.parametrize(
    "symbol",
    ["$MEME", "wrapped eth", "A/B", "ETH!"],
)
def test_key_survives_a_hostile_symbol(symbol: str) -> None:
    # Symbols come from token metadata, which is attacker-supplied on-chain
    # text. The key lands in an issue title and a state filename, so it must
    # normalise to one safe token — and stay stable across restatements of the
    # same symbol.
    key = alerts_pending.build_alert_key(symbol, "depeg")
    assert key.startswith("news-")
    assert key.endswith("-depeg")
    assert "--" not in key
    assert all(ch.isalnum() or ch == "-" for ch in key)


# ------------------------------------------------------------- JSON payload


def test_payload_carries_everything_the_alert_text_needs() -> None:
    payload = alerts_pending.row_to_payload(_row())
    # The dispatcher never queries the database, so anything missing here is
    # missing from the push.
    assert payload == {
        "id": "11111111-1111-4111-8111-111111111111",
        "key": "news-eth-hack-exploit",
        "category": "hack_exploit",
        "severity": "critical",
        "summary": "Bridge exploit drains 40M",
        "classifier": "heuristic",
        "created_at": "2026-10-16T10:00:00+00:00",
        "asset_symbol": "ETH",
        "asset_name": "Ether",
        "title": "Exploit hits bridge",
        "url": "https://example.test/a",
        "news_site": "example.test",
        "published_at": "2026-10-16T09:55:00+00:00",
    }
    # Round-trips: stdout is parsed by python3 inside the dispatcher.
    assert json.loads(json.dumps(payload)) == payload


def test_payload_tolerates_a_missing_publication_date() -> None:
    payload = alerts_pending.row_to_payload(_row(published_at=None))
    assert payload["published_at"] is None


# ------------------------------------------------------- degradation paths


async def test_missing_table_reports_nothing_pending() -> None:
    # Before migration 0033 reaches a stand this must be a quiet no-op, because
    # that is what lets the timer be installed and proven firing ahead of the
    # table existing instead of being the last unproven step.
    session = _StubSession(error=_programming_error("42P01"))
    assert await alerts_pending.fetch_pending(session) == []  # type: ignore[arg-type]


async def test_other_database_errors_are_not_swallowed() -> None:
    # A syntax error or a renamed column must not look like "nothing pending" —
    # that would mean alerts silently stop being delivered.
    session = _StubSession(error=_programming_error("42703"))  # undefined_column
    with pytest.raises(ProgrammingError):
        await alerts_pending.fetch_pending(session)  # type: ignore[arg-type]


async def test_limit_is_passed_through_and_bounded_by_default() -> None:
    session = _StubSession(rows=[_row()])
    alerts = await alerts_pending.fetch_pending(session, limit=5)  # type: ignore[arg-type]
    assert len(alerts) == 1
    assert session.params == {"limit": 5}
    assert alerts_pending.parse_args([]).limit == alerts_pending.DEFAULT_LIMIT


# -------------------------------------------------------------- marking


def test_ids_must_be_uuids() -> None:
    # These arrive from a shell loop. A mangled argument list has to fail
    # visibly rather than quietly update nothing.
    with pytest.raises(ValueError):
        alerts_mark.parse_ids(["not-a-uuid"])


def test_ids_round_trip() -> None:
    raw = ["11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"]
    assert [str(i) for i in alerts_mark.parse_ids(raw)] == raw


async def test_marking_dispatched_commits_and_counts() -> None:
    session = _StubSession(rows=[_row()])
    changed = await alerts_mark.mark_dispatched(  # type: ignore[arg-type]
        session, alerts_mark.parse_ids(["11111111-1111-4111-8111-111111111111"])
    )
    assert changed == 1
    assert session.params == {"ids": ["11111111-1111-4111-8111-111111111111"]}


async def test_dispatch_error_is_truncated_before_it_reaches_the_column() -> None:
    session = _StubSession(rows=[_row()])
    await alerts_mark.mark_error(  # type: ignore[arg-type]
        session, alerts_mark.parse_ids(["11111111-1111-4111-8111-111111111111"]), "x" * 5000
    )
    assert session.params is not None
    assert len(str(session.params["error"])) == alerts_mark.ERROR_MAX_CHARS


def test_error_mode_is_explicit() -> None:
    # Without --error the default must be "dispatched", because the error path
    # leaves the row pending and a mixed-up default would either lose alerts or
    # resend them forever.
    assert alerts_mark.parse_args(["11111111-1111-4111-8111-111111111111"]).error is None
    assert (
        alerts_mark.parse_args(["--error", "boom", "11111111-1111-4111-8111-111111111111"]).error
        == "boom"
    )
