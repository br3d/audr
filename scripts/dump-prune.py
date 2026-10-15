#!/usr/bin/env python3
"""GFS pruner for the Paperclip DB dump directory on ai-stuff (AUD-435).

WHY THIS EXISTS
---------------
The Paperclip control plane dumps its database hourly into
`/home/codex/.paperclip/instances/default/data/backups` on ai-stuff
(192.168.1.152). Each dump is ~95 MB, so the directory grows ~2.1 GB/day and
twice took the host above 85% disk.

The durable fix is a board setting (`dailyDays: 3`) that only the platform
owner can write — an agent token gets 403. AUD-435 is parked on that click.
Until it lands, the Chief of Staff authorised (AUD-500, 2026-10-11) reaching
the same already-approved end state by deleting files directly, with the
explicit caveat that the volume re-grows to ~15 GB in about a week and the
prune has to be repeated.

The first repeat was done by hand: a one-off delete set derived file-by-file,
re-checked against the live directory, 97 files / 8.01 GiB. Doing that again
every week by hand is how a careful rule turns into an accident. This script is
that rule, executable and tested, so each repeat is a reviewed dry-run and one
`--apply` rather than fresh arithmetic.

RETENTION RULE (a dump survives if EITHER applies)
--------------------------------------------------
  * its calendar date is within the last --days days, counting today as day 1
    — i.e. with the default 3, everything dated today, yesterday or the day
    before, every hour of it. This is the "daily tier" the board setting names,
    and it is what `dailyDays: 3` keeps: whole days of hourlies, not one dump
    per day;
  * it is the NEWEST dump of its ISO week, among dumps older than that window.
    This is the GFS weekly promotion the server's own prune performs, and it is
    why pruning never leaves a week with no recoverable dump. `--weeks N` caps
    how many of those survivors are kept, newest weeks first; uncapped by
    default, because that is the AUD-435 rule for this archive. Any directory a
    timer keeps writing to wants the cap — see the note on it in `plan()`.

Everything else is deleted. Measured against the live directory the rule holds
~6-7 GB in steady state (5 weekly survivors ~0.7 GB + 3 days of hourlies
~6.3 GB), which is the ~6 GB target authorised on AUD-458.

WHAT IT WILL NOT DO
-------------------
Files whose names do not match the dump pattern are never candidates — they are
counted and reported, never deleted. The newest dump in the directory is
protected unconditionally, so a clock skew or a bad --days cannot empty the
directory. Nothing is deleted without --apply; dry-run is the default.

It also touches nothing else on the host: no Docker prune, no config edit, no
service restart. `/` pressure on ai-stuff has several contributors and the
Docker-side GC is separately tuned (scripts/host-gc.sh) — do not reach for this
script to fix a shortage the dumps did not cause. Check `sudo du -sh` on the
directory first.

USAGE
-----
    python3 scripts/dump-prune.py                 # dry run, default directory
    python3 scripts/dump-prune.py --apply         # delete
    python3 scripts/dump-prune.py --days 7        # a wider daily tier

Reading the directory needs root (the dumps are mode 600), so in practice:
`sudo python3 scripts/dump-prune.py`.

A SECOND DUMP FAMILY (AUD-562)
------------------------------
audr's own scheduled backups land in `/home/codex/audr/backups` on the deploy
host as `audr-<ts>.sql.age`. They are pruned by the same rule through
`--prefix`, driven from `scripts/backup-run.sh`:

    python3 scripts/dump-prune.py --dir /home/codex/audr/backups \\
        --prefix audr --days 7 --weeks 4 --apply

Generalising this script rather than writing a second pruner is deliberate:
there is one retention rule here whose failure mode is a deleted backup, and it
should have one implementation and one test suite. Note that directory also
holds `master_key-<ts>.hex` key copies when an operator takes a manual dump —
those never match a dump pattern and are never candidates, which the tests
assert explicitly.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import sys

DEFAULT_DIR = "/home/codex/.paperclip/instances/default/data/backups"
DEFAULT_DAYS = 3
DEFAULT_PREFIX = "paperclip"


def dump_re(prefix: str) -> re.Pattern[str]:
    """Filename pattern for one dump family: `<prefix>-YYYYmmdd-HHMMSS.sql[.ext]`.

    Both dump producers we own write that shape:

      * `paperclip-20261011-201039.sql.gz` — the Paperclip control plane. The
        uncompressed `.sql` spelling also exists in the archive (the 2026-09-12
        dump predates compression), so both are dumps.
      * `audr-20261015-141500.sql.gpg` — `scripts/backup.sh` (AUD-390), `.age`
        when `age` is installed and `.gpg` on the fallback path.

    `--prefix` exists so the second family reuses this rule rather than getting
    a pruner of its own (AUD-562). The prefix is quoted: it names a file, and a
    caller passing `audr.old` must not match `audrXold`.
    """
    return re.compile(rf"^{re.escape(prefix)}-(\d{{8}})-(\d{{6}})\.sql(?:\.gz|\.age|\.gpg)?$")

KEEP_RECENT = "recent"
KEEP_WEEKLY = "weekly"
KEEP_NEWEST = "newest"


def parse_stamp(name: str, pattern: re.Pattern[str] | None = None) -> dt.datetime | None:
    """Return the timestamp encoded in a dump filename, or None if not a dump."""
    m = (pattern or dump_re(DEFAULT_PREFIX)).match(name)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        # A name-shaped file with an impossible date (20261332-...) is not a
        # dump we understand, and so not ours to delete.
        return None


def survey(
    directory: pathlib.Path,
    pattern: re.Pattern[str] | None = None,
) -> tuple[list[tuple[str, dt.datetime, int]], list[str]]:
    """Split a directory into (dumps, unrecognised names).

    `dumps` is [(name, stamp, size_bytes)] sorted newest first.
    """
    dumps: list[tuple[str, dt.datetime, int]] = []
    other: list[str] = []
    for entry in sorted(os.scandir(directory), key=lambda e: e.name):
        if not entry.is_file(follow_symlinks=False):
            other.append(entry.name)
            continue
        stamp = parse_stamp(entry.name, pattern)
        if stamp is None:
            other.append(entry.name)
            continue
        dumps.append((entry.name, stamp, entry.stat().st_size))
    dumps.sort(key=lambda d: d[1], reverse=True)
    return dumps, other


def plan(
    dumps: list[tuple[str, dt.datetime, int]],
    days: int,
    today: dt.date,
    weeks: int | None = None,
) -> list[tuple[str, int, str | None]]:
    """Decide the fate of every dump.

    Returns [(name, size, keep_reason_or_None)] in the input order. A None
    reason means "delete". Pure function of its arguments: no filesystem, no
    clock — that is what makes the rule testable.
    """
    if days < 1:
        raise ValueError("--days must be >= 1")
    if weeks is not None and weeks < 1:
        raise ValueError("--weeks must be >= 1 when given")

    cutoff = today - dt.timedelta(days=days - 1)

    # Newest dump of each ISO week, computed over the dumps that fall outside
    # the daily tier. Restricting it to those is deliberate: a week that the
    # daily tier already covers needs no separate survivor, and promoting one
    # would protect an arbitrary hour of a week we keep in full anyway.
    weekly_survivor: dict[tuple[int, int], dt.datetime] = {}
    for _, stamp, _ in dumps:
        if stamp.date() >= cutoff:
            continue
        key = stamp.isocalendar()[:2]
        if stamp > weekly_survivor.get(key, dt.datetime.min):
            weekly_survivor[key] = stamp

    # Cap on how many of those weekly survivors are kept, newest weeks first.
    #
    # Without a cap the weekly tier grows forever: one dump per ISO week, never
    # deleted. That was affordable for the Paperclip dumps this script was
    # written for, so the default stays uncapped and their timer's behaviour is
    # unchanged. It is NOT affordable for audr's own dumps — measured at 282 MiB
    # each (AUD-562, pg_dump is not compressed before `age` encrypts it), an
    # uncapped weekly tier adds ~14.7 GiB a year to a deploy host with 11 GiB
    # free, i.e. a disk-full in roughly eight months. That is a worse outage
    # than the one backups prevent, and it arrives silently.
    #
    # Dropping the oldest weekly survivor is cheap in a way worth saying out
    # loud: these dumps are a RECOVERY point, not an archive. The live database
    # holds the full valuation history, and restoring a two-month-old snapshot
    # of a portfolio tracker is close to useless anyway — the recovery cases
    # this schedule exists for (volume loss, a bad migration, an accidental
    # delete) are all noticed in hours or days, not months.
    if weeks is not None and len(weekly_survivor) > weeks:
        kept_weeks = sorted(weekly_survivor, key=lambda k: weekly_survivor[k], reverse=True)[:weeks]
        weekly_survivor = {k: weekly_survivor[k] for k in kept_weeks}

    newest = max((stamp for _, stamp, _ in dumps), default=None)

    decided: list[tuple[str, int, str | None]] = []
    for name, stamp, size in dumps:
        if stamp == newest:
            # Unconditional: never leave the directory without its latest dump,
            # whatever --days or the system clock say.
            reason: str | None = KEEP_RECENT if stamp.date() >= cutoff else KEEP_NEWEST
        elif stamp.date() >= cutoff:
            reason = KEEP_RECENT
        elif weekly_survivor.get(stamp.isocalendar()[:2]) == stamp:
            reason = KEEP_WEEKLY
        else:
            reason = None
        decided.append((name, size, reason))
    return decided


def gib(n: int) -> str:
    return f"{n / 2**30:.2f} GiB"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Prune a DB dump directory to the authorised GFS retention"
        " (AUD-435 for Paperclip dumps, AUD-562 for audr's own).",
    )
    ap.add_argument("--dir", default=DEFAULT_DIR, help=f"dump directory (default {DEFAULT_DIR})")
    ap.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="dump filename prefix before the timestamp, e.g. 'audr' for"
        f" audr-20261015-141500.sql.gpg (default {DEFAULT_PREFIX})",
    )
    ap.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"keep every dump from the last N calendar days (default {DEFAULT_DAYS})",
    )
    ap.add_argument(
        "--weeks",
        type=int,
        default=None,
        help="keep at most N weekly survivors, newest first (default: unlimited,"
        " which is the AUD-435 rule for the Paperclip archive). Set this for any"
        " directory a timer keeps writing to, or the weekly tier grows forever.",
    )
    ap.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    ap.add_argument(
        "--today",
        default=None,
        help="override today's date as YYYY-MM-DD (for reproducing a past run)",
    )
    args = ap.parse_args(argv)

    directory = pathlib.Path(args.dir)
    if not directory.is_dir():
        print(f"error: {directory} is not a directory", file=sys.stderr)
        return 2

    pattern = dump_re(args.prefix)
    try:
        dumps, other = survey(directory, pattern)
    except PermissionError as exc:
        print(f"error: cannot read {directory}: {exc}", file=sys.stderr)
        print("the dumps are mode 600 — re-run under sudo", file=sys.stderr)
        return 2

    if not dumps:
        print(f"{directory}: no dumps matching {pattern.pattern} — nothing to do")
        return 0

    try:
        today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
        decided = plan(dumps, args.days, today, args.weeks)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    doomed = [(n, s) for n, s, why in decided if why is None]
    kept = [(n, s, why) for n, s, why in decided if why is not None]
    total = sum(s for _, s, _ in decided)
    freed = sum(s for _, s in doomed)

    print(f"{directory}")
    print(f"  dumps        {len(dumps)} files, {gib(total)}")
    if other:
        print(
            f"  not a dump   {len(other)} entries, left alone: {', '.join(other[:5])}"
            + (" ..." if len(other) > 5 else "")
        )
    print(
        f"  rule         keep all of the last {args.days} days (today = {today}),"
        " plus the newest dump of each older ISO week"
        + (f" (at most {args.weeks} of those)" if args.weeks is not None else "")
    )
    print(f"  keep         {len(kept)} files, {gib(total - freed)}")
    for name, _, why in sorted(kept, key=lambda k: k[0]):
        if why != KEEP_RECENT:
            print(f"                 {name}  [{why}]")
    recent = sum(1 for _, _, why in kept if why == KEEP_RECENT)
    if recent:
        print(f"                 (+{recent} in the {args.days}-day daily tier)")
    print(f"  delete       {len(doomed)} files, {gib(freed)}")

    if not doomed:
        print("\nalready at the retention target — nothing to delete")
        return 0

    if not args.apply:
        print("\ndry run — re-run with --apply to delete")
        return 0

    deleted = errors = 0
    for name, _ in doomed:
        try:
            (directory / name).unlink()
            deleted += 1
        except OSError as exc:
            print(f"  failed: {name}: {exc}", file=sys.stderr)
            errors += 1
    print(
        f"\ndeleted {deleted} files, {gib(freed)} freed"
        + (f"; {errors} failures" if errors else "")
    )
    if args.prefix == DEFAULT_PREFIX:
        print(
            "note: this runs daily from audr-dump-prune.timer (AUD-505/AUD-509), so"
            " no hand-repeat is due. Paperclip still writes ~2.1 GB/day of dumps that"
            " this deletes again; reducing that write churn needs the retention"
            " setting on the Paperclip board (AUD-435), which only a board user can"
            " change. Disk stays flat either way."
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
