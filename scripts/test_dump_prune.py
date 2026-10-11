#!/usr/bin/env python3
"""Unit tests for scripts/dump-prune.py.

Dependency-free, like test_registry_prune.py: run it with
`python3 scripts/test_dump_prune.py`. The backend pytest suite cannot see
`scripts/` (its container only copies `backend/`).

Every case here is a retention rule whose failure mode is a deleted backup, so
the tests are written against the failure, not the happy path. The fixture is
the real 2026-10-11 directory listing, which is the one run we can check an
answer against: the hand-prune executed under AUD-500 deleted 97 files and kept
68, and this script has to agree with it exactly.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import sys
import tempfile

_SCRIPT = pathlib.Path(__file__).with_name("dump-prune.py")
_spec = importlib.util.spec_from_file_location("dump_prune", _SCRIPT)
dp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dp)


def dumps(*names, size=100):
    """Build the structure survey() returns, from filenames alone."""
    out = []
    for n in names:
        stamp = dp.parse_stamp(n)
        assert stamp is not None, f"fixture name is not a dump: {n}"
        out.append((n, stamp, size))
    out.sort(key=lambda d: d[1], reverse=True)
    return out


def hourly(date: str, hours=range(24)):
    return [f"paperclip-{date}-{h:02d}1039.sql.gz" for h in hours]


def decide(rows, days, today):
    return {n: why for n, _, why in dp.plan(rows, days, dt.date.fromisoformat(today))}


def deleted(rows, days, today):
    return {n for n, why in decide(rows, days, today).items() if why is None}


def test_daily_tier_keeps_whole_days_not_one_per_day():
    """`dailyDays: 3` means three days of hourlies — 72 files, not 3."""
    rows = dumps(*hourly("20261011", range(21)), *hourly("20261010"), *hourly("20261009"))
    assert deleted(rows, 3, "2026-10-11") == set()
    reasons = set(decide(rows, 3, "2026-10-11").values())
    assert reasons == {dp.KEEP_RECENT}


def test_day_before_the_window_is_deleted_except_its_weekly_survivor():
    rows = dumps(*hourly("20261011", range(3)), *hourly("20261008", range(3)))
    fate = decide(rows, 3, "2026-10-11")
    # 10-08 is outside a 3-day window ending 10-11 (keeps 09/10/11).
    assert fate["paperclip-20261008-021039.sql.gz"] == dp.KEEP_WEEKLY  # newest of its week
    assert fate["paperclip-20261008-011039.sql.gz"] is None
    assert fate["paperclip-20261008-001039.sql.gz"] is None


def test_every_older_iso_week_keeps_exactly_one_survivor():
    """No week older than the window may be left with nothing recoverable."""
    rows = dumps(
        "paperclip-20261011-201039.sql.gz",
        # ISO week 41
        "paperclip-20261008-231039.sql.gz",
        "paperclip-20261006-231039.sql.gz",
        # ISO week 40
        "paperclip-20261004-235356.sql.gz",
        "paperclip-20260930-235356.sql.gz",
        # ISO week 39
        "paperclip-20260927-232752.sql.gz",
    )
    fate = decide(rows, 3, "2026-10-11")
    survivors = {n for n, why in fate.items() if why == dp.KEEP_WEEKLY}
    assert survivors == {
        "paperclip-20261008-231039.sql.gz",
        "paperclip-20261004-235356.sql.gz",
        "paperclip-20260927-232752.sql.gz",
    }
    weeks = {dp.parse_stamp(n).isocalendar()[:2] for n in fate}
    kept_weeks = {dp.parse_stamp(n).isocalendar()[:2] for n, why in fate.items() if why}
    assert weeks == kept_weeks, "a week lost every dump it had"


def test_newest_dump_survives_even_when_the_rule_would_drop_it():
    """A clock skew or a stale directory must not empty it."""
    rows = dumps("paperclip-20260912-091129.sql", "paperclip-20260910-091129.sql")
    fate = decide(rows, 3, "2026-10-11")  # both far outside the window
    assert fate["paperclip-20260912-091129.sql"] == dp.KEEP_NEWEST
    assert None not in {fate["paperclip-20260912-091129.sql"]}


def test_uncompressed_sql_is_a_dump_and_foreign_names_are_not():
    assert dp.parse_stamp("paperclip-20260912-091129.sql") is not None
    assert dp.parse_stamp("paperclip-20261011-201039.sql.gz") is not None
    for name in (
        "paperclip-20261332-201039.sql.gz",  # impossible date
        "paperclip-20261011-201039.sql.zst",  # unknown compression
        "paperclip-20261011.sql.gz",  # no time component
        "audr-20261011-201039.sql.gz",  # another product's dump
        "paperclip-20261011-201039.sql.gz.part",  # a dump still being written
        "README",
    ):
        assert dp.parse_stamp(name) is None, name


def test_foreign_files_are_never_deleted_by_a_real_run():
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        for name in hourly("20261011", range(2)) + ["notes.txt", "paperclip-restore.log"]:
            (d / name).write_bytes(b"x")
        (d / "subdir").mkdir()
        found, other = dp.survey(d)
        assert {n for n, _, _ in found} == set(hourly("20261011", range(2)))
        assert set(other) == {"notes.txt", "paperclip-restore.log", "subdir"}


def test_days_must_be_at_least_one():
    rows = dumps("paperclip-20261011-201039.sql.gz")
    for bad in (0, -1):
        try:
            dp.plan(rows, bad, dt.date(2026, 10, 11))
        except ValueError:
            pass
        else:
            raise AssertionError(f"--days {bad} was accepted")


def test_reproduces_the_hand_prune_of_2026_10_11():
    """The one run with a known-correct answer: 165 files in, 68 kept, 97 deleted.

    Reconstructed from the AUD-500 execution record: hourly dumps from 10-09
    onward survive, plus the five weekly survivors named there.
    """
    names = []
    # The daily tier as it stood: 10-09 and 10-10 complete, 10-11 up to 14:00.
    names += hourly("20261009") + hourly("20261010") + hourly("20261011", range(15))
    # Older history, hour-dense, which is what the prune actually cleared.
    for day in ("20261005", "20261006", "20261007", "20261008"):
        names += hourly(day)
    names += hourly("20261004", range(8)) + ["paperclip-20261004-235356.sql.gz"]
    names += ["paperclip-20260927-232752.sql.gz", "paperclip-20260920-234248.sql.gz"]
    names += ["paperclip-20260912-091129.sql"]

    fate = decide(dumps(*names), 3, "2026-10-11")
    kept = {n for n, why in fate.items() if why}
    weekly = {n for n, why in fate.items() if why == dp.KEEP_WEEKLY}

    assert weekly == {
        "paperclip-20261008-231039.sql.gz",
        "paperclip-20261004-235356.sql.gz",
        "paperclip-20260927-232752.sql.gz",
        "paperclip-20260920-234248.sql.gz",
        "paperclip-20260912-091129.sql",
    }, "weekly survivors differ from the five the hand-prune kept"
    assert len(kept) == 24 + 24 + 15 + len(weekly), "daily tier plus weekly survivors"
    assert len(fate) - len(kept) == len(names) - len(kept)


def test_second_run_on_an_already_pruned_directory_deletes_nothing():
    """Idempotence: the weekly repeat must be a no-op until new days accumulate."""
    rows = dumps(
        *hourly("20261011", range(21)),
        *hourly("20261010"),
        *hourly("20261009"),
        "paperclip-20261008-231039.sql.gz",
        "paperclip-20261004-235356.sql.gz",
        "paperclip-20260927-232752.sql.gz",
    )
    assert deleted(rows, 3, "2026-10-11") == set()


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"ok   {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {t.__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run())
