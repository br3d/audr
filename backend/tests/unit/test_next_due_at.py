"""Unit tests for `_next_due_at` — the Status page "Next scheduled runs" value.

AUD-456: the card rendered blank because the API read `schedule.next_run_at`,
a column the dispatcher never writes. The value is now derived the same way
`is_due()` decides readiness: `last_run_at + freshness_s`.
"""

from datetime import UTC, datetime, timedelta

from audr.api.settings import _next_due_at


def _call(**overrides: object) -> str | None:
    kwargs: dict[str, object] = {
        "enabled": True,
        "paused_at": None,
        "freshness_s": 3600,
        "last_run_at": datetime(2026, 10, 6, 12, 0, tzinfo=UTC),
        "next_run_at": None,
    }
    kwargs.update(overrides)
    return _next_due_at(**kwargs)  # type: ignore[arg-type]


def test_derives_from_last_run_plus_freshness() -> None:
    assert _call() == datetime(2026, 10, 6, 13, 0, tzinfo=UTC).isoformat()


def test_explicit_next_run_at_wins() -> None:
    explicit = datetime(2026, 10, 7, 9, 30, tzinfo=UTC)
    assert _call(next_run_at=explicit) == explicit.isoformat()


def test_disabled_returns_none() -> None:
    assert _call(enabled=False) is None


def test_paused_returns_none() -> None:
    assert _call(paused_at=datetime(2026, 10, 1, tzinfo=UTC)) is None


def test_never_run_is_due_now() -> None:
    result = _call(last_run_at=None)
    assert result is not None
    assert datetime.fromisoformat(result) <= datetime.now(UTC) + timedelta(seconds=5)


def test_no_freshness_window_is_due_now() -> None:
    result = _call(freshness_s=None)
    assert result is not None
    assert datetime.fromisoformat(result) <= datetime.now(UTC) + timedelta(seconds=5)
