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


def dumps(*names, size=100, prefix="paperclip"):
    """Build the structure survey() returns, from filenames alone."""
    pattern = dp.dump_re(prefix)
    out = []
    for n in names:
        stamp = dp.parse_stamp(n, pattern)
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


# --- the audr dump family (AUD-562) -----------------------------------------
#
# The same rule, a second producer. These cases exist because generalising a
# pruner is where you silently widen what it is willing to delete: the rule is
# shared on purpose, the *candidate set* must not be. The on-host audr
# directory also holds `master_key-<ts>.hex` copies, and a pruner that ever
# treats one of those as a dump destroys the only thing that makes the dumps
# readable.


def audr_daily(dates, ext=".age"):
    """One dump per listed date, as the scheduled timer writes them."""
    return [f"audr-{d}-033012.sql{ext}" for d in dates]


def test_audr_dumps_match_only_under_their_own_prefix():
    """Neither family may be a candidate while pruning the other."""
    audr = dp.dump_re("audr")
    paperclip = dp.dump_re("paperclip")
    assert dp.parse_stamp("audr-20261015-141500.sql.age", audr) is not None
    assert dp.parse_stamp("audr-20261015-141500.sql.gpg", audr) is not None
    # The pruner that runs on ai-stuff must never reach an audr dump, and the
    # one on the deploy host must never reach a Paperclip dump.
    assert dp.parse_stamp("audr-20261015-141500.sql.age", paperclip) is None
    assert dp.parse_stamp("paperclip-20261011-201039.sql.gz", audr) is None


def test_audr_key_copies_are_never_dumps():
    """`master_key-<ts>.hex` sits in the same directory. Deleting one is fatal.

    Not "fatal" as in a lost backup — as in every *surviving* dump becomes
    unreadable for wallet addresses and provider credentials (AUD-490).
    """
    audr = dp.dump_re("audr")
    for name in (
        "master_key-20261015-141500.hex",
        "audr-20261015-141500.hex",
        "audr-20261015-141500.sql.age.part",  # still being written
        "audr-20261015.sql.age",  # no time component
        "audr-20261332-141500.sql.age",  # impossible date
        "audr-restore.log",
        "backup_key.txt",
    ):
        assert dp.parse_stamp(name, audr) is None, name


def test_audr_directory_survey_leaves_the_key_copies_alone():
    """End to end on a real directory, not just the regex."""
    with tempfile.TemporaryDirectory() as tmp:
        d = pathlib.Path(tmp)
        keepers = {"master_key-20261014-033012.hex", "master_key-20261015-033012.hex"}
        dumps_on_disk = set(audr_daily(["20261014", "20261015"]))
        for name in dumps_on_disk | keepers:
            (d / name).write_bytes(b"x")
        found, other = dp.survey(d, dp.dump_re("audr"))
        assert {n for n, _, _ in found} == dumps_on_disk
        assert set(other) == keepers


def test_audr_daily_schedule_keeps_the_window_and_one_dump_per_older_week():
    """The shape the timer actually produces: one dump a day, --days 14.

    Asserted as properties rather than a hand-counted list, because the point
    of the rule is that no week is ever left with nothing recoverable.
    """
    start, today = dt.date(2026, 9, 1), dt.date(2026, 11, 1)
    dates = [
        (start + dt.timedelta(days=i)).strftime("%Y%m%d")
        for i in range((today - start).days + 1)
    ]
    rows = dumps(*audr_daily(dates), prefix="audr")
    fate = decide(rows, 14, today.isoformat())

    cutoff = today - dt.timedelta(days=13)
    recent = {n for n, why in fate.items() if why == dp.KEEP_RECENT}
    assert len(recent) == 14, "the 14-day daily tier is 14 daily dumps"
    assert all(dp.parse_stamp(n, dp.dump_re("audr")).date() >= cutoff for n in recent)

    # Every ISO week present survives, and the older ones by exactly one dump.
    def week(name):
        return dp.parse_stamp(name, dp.dump_re("audr")).isocalendar()[:2]

    kept = {n for n, why in fate.items() if why}
    assert {week(n) for n in fate} == {week(n) for n in kept}, "a week lost every dump"
    for w in {week(n) for n in fate} - {week(n) for n in recent}:
        survivors = {n for n in kept if week(n) == w}
        assert len(survivors) == 1, f"week {w} kept {len(survivors)} dumps, expected 1"
        assert survivors == {max((n for n in fate if week(n) == w))}, "not the newest"


def test_audr_newest_dump_survives_a_stale_directory():
    """A stand that stopped backing up must not have its last dump pruned."""
    rows = dumps(*audr_daily(["20260901", "20260902"]), prefix="audr")
    fate = decide(rows, 14, "2026-11-01")
    assert fate["audr-20260902-033012.sql.age"] == dp.KEEP_NEWEST


# --- the weekly-tier cap (AUD-562) ------------------------------------------


def test_uncapped_weekly_tier_is_still_the_default():
    """The Paperclip timer's behaviour must be bit-identical without --weeks."""
    rows = dumps(
        "paperclip-20261011-201039.sql.gz",
        "paperclip-20261008-231039.sql.gz",
        "paperclip-20261004-235356.sql.gz",
        "paperclip-20260927-232752.sql.gz",
        "paperclip-20260920-234248.sql.gz",
    )
    today = dt.date(2026, 10, 11)
    assert dp.plan(rows, 3, today) == dp.plan(rows, 3, today, None)
    weekly = {n for n, _, why in dp.plan(rows, 3, today) if why == dp.KEEP_WEEKLY}
    assert len(weekly) == 4, "every older week survives when uncapped"


def test_weeks_cap_keeps_the_newest_weeks_and_drops_the_oldest():
    """Bounded disk: at 282 MiB a dump, an uncapped weekly tier fills the host."""
    rows = dumps(
        *audr_daily(["20261015"]),
        *audr_daily(["20261008", "20261001", "20260924", "20260917", "20260910"]),
        prefix="audr",
    )
    fate = decide(rows, 7, "2026-10-15")  # uncapped baseline
    assert len([n for n, why in fate.items() if why == dp.KEEP_WEEKLY]) == 5

    capped = {
        n: why for n, _, why in dp.plan(rows, 7, dt.date(2026, 10, 15), 2)
    }
    assert {n for n, why in capped.items() if why == dp.KEEP_WEEKLY} == {
        "audr-20261008-033012.sql.age",
        "audr-20261001-033012.sql.age",
    }, "the cap must keep the NEWEST weeks, not the first ones encountered"
    assert {n for n, why in capped.items() if why is None} == {
        "audr-20260924-033012.sql.age",
        "audr-20260917-033012.sql.age",
        "audr-20260910-033012.sql.age",
    }


def test_weeks_cap_bounds_the_directory_in_steady_state():
    """Two years of daily dumps must not exceed daily + capped weekly tiers."""
    start, today = dt.date(2024, 11, 1), dt.date(2026, 11, 1)
    dates = [
        (start + dt.timedelta(days=i)).strftime("%Y%m%d")
        for i in range((today - start).days + 1)
    ]
    rows = dumps(*audr_daily(dates), size=282 * 2**20, prefix="audr")
    decided = dp.plan(rows, 7, today, 4)
    kept = [(n, s) for n, s, why in decided if why]
    assert len(kept) == 7 + 4, f"steady state is 11 dumps, got {len(kept)}"
    # The number the host's free space has to cover, and it must not drift.
    assert sum(s for _, s in kept) / 2**30 < 3.2, "steady state grew past ~3.1 GiB"


def test_weeks_cap_can_never_empty_the_directory():
    """A cap plus a stalled timer must still leave the last dump behind."""
    rows = dumps(*audr_daily(["20260910", "20260917"]), prefix="audr")
    fate = {n: why for n, _, why in dp.plan(rows, 7, dt.date(2026, 11, 1), 1)}
    assert fate["audr-20260917-033012.sql.age"] == dp.KEEP_NEWEST
    assert any(why for why in fate.values())


def test_weeks_must_be_at_least_one_when_given():
    rows = dumps(*audr_daily(["20261015"]), prefix="audr")
    for bad in (0, -1):
        try:
            dp.plan(rows, 7, dt.date(2026, 10, 15), bad)
        except ValueError:
            pass
        else:
            raise AssertionError(f"--weeks {bad} was accepted")


def test_prefix_is_matched_literally_not_as_a_pattern():
    """A prefix is a filename, not a regex — `audr.old` must not match `audrXold`."""
    pattern = dp.dump_re("audr.old")
    assert dp.parse_stamp("audr.old-20261015-141500.sql.age", pattern) is not None
    assert dp.parse_stamp("audrXold-20261015-141500.sql.age", pattern) is None


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
