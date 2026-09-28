"""Unit tests for schedule-patch validation helpers (AUD-272).

Covers:
- _seconds_to_cron / _cron_to_seconds round-trip for valid whole-minute values
- _validate_schedule_patch rejects: zero, negative, sub-60, non-multiple-of-60 intervals
- _validate_schedule_patch rejects: zero or negative freshness_seconds
- _validate_schedule_patch passes: valid whole-minute intervals and positive freshness
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from audr.api.settings import (
    SettingsSchedulePatch,
    _cron_to_seconds,
    _seconds_to_cron,
    _validate_schedule_patch,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Cron round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seconds",
    [60, 120, 300, 600, 1800, 3600, 7200, 14400, 86400, 172800],
)
def test_cron_round_trip(seconds: int) -> None:
    """_cron_to_seconds(_seconds_to_cron(s)) == s for valid whole-minute values."""
    assert _cron_to_seconds(_seconds_to_cron(seconds)) == seconds


# ---------------------------------------------------------------------------
# _validate_schedule_patch — interval_seconds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_interval", [0, -1, -60, -3600])
def test_validate_patch_rejects_non_positive_interval(bad_interval: int) -> None:
    """`interval_seconds` <= 0 must raise HTTP 422."""
    with pytest.raises(HTTPException) as exc_info:
        _validate_schedule_patch(SettingsSchedulePatch(interval_seconds=bad_interval))
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("bad_interval", [1, 30, 59, 90, 119, 150])
def test_validate_patch_rejects_sub_minute_or_non_multiple(bad_interval: int) -> None:
    """`interval_seconds` that are not a positive multiple of 60 must raise HTTP 422."""
    with pytest.raises(HTTPException) as exc_info:
        _validate_schedule_patch(SettingsSchedulePatch(interval_seconds=bad_interval))
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("valid_interval", [60, 120, 300, 3600, 7200, 86400])
def test_validate_patch_accepts_whole_minute_interval(valid_interval: int) -> None:
    """`interval_seconds` that is a positive multiple of 60 must not raise."""
    _validate_schedule_patch(SettingsSchedulePatch(interval_seconds=valid_interval))


# ---------------------------------------------------------------------------
# _validate_schedule_patch — freshness_seconds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_freshness", [0, -1, -300])
def test_validate_patch_rejects_non_positive_freshness(bad_freshness: int) -> None:
    """`freshness_seconds` <= 0 must raise HTTP 422."""
    with pytest.raises(HTTPException) as exc_info:
        _validate_schedule_patch(SettingsSchedulePatch(freshness_seconds=bad_freshness))
    assert exc_info.value.status_code == 422


@pytest.mark.parametrize("valid_freshness", [1, 30, 60, 300, 3600])
def test_validate_patch_accepts_positive_freshness(valid_freshness: int) -> None:
    """`freshness_seconds` > 0 must not raise."""
    _validate_schedule_patch(SettingsSchedulePatch(freshness_seconds=valid_freshness))


def test_validate_patch_accepts_none_values() -> None:
    """A patch with all None fields (no changes) must not raise."""
    _validate_schedule_patch(SettingsSchedulePatch())


def test_validate_patch_accepts_enabled_only() -> None:
    """A patch that only changes `enabled` must not raise."""
    _validate_schedule_patch(SettingsSchedulePatch(enabled=True))
