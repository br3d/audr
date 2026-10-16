"""The watchdog's secondary checks: disk (AUD-580), dispatch lag (AUD-592), spool (AUD-595).

`scripts/host-gc.sh` has always been able to *detect* disk pressure — it has two
escalating WARN paths for it — but it only ever `echo`s, from an `if: always()`
step inside a CI job whose stdout never reaches the journal. Nothing alerted. So
the condition host-gc calls "Docker is no longer the thing filling this disk"
was invisible, on a host whose own GC header notes that exhaustion "takes the
live stack's database volume down with it".

The dispatch-lag check (AUD-592) covers the same shape one layer up:
`scripts/alert-dispatch.sh` exits 0 on any unreadable stack by design, so a
bridge that cannot deliver a single alert leaves a green unit behind it. That
state was real after AUD-584 phase 2 merged — the timer ticked for an hour
against an image with no `audr.jobs.alerts_pending`, and it was found only by
reading the journal by eye.

The spool check (AUD-595) is the other half of that same layer: because
notify.sh exits 0 when it only *spools* an alert, the dispatch-lag check cannot
see the road where nothing reached a human at all. Its artefact is the age of
the oldest line in `undelivered.jsonl`.

These tests drive the real `scripts/stand-watchdog.sh` (bind-mounted by
compose.test.yaml) with a stubbed `df` and `docker` on PATH and a stubbed
`notify.sh`, so what is asserted is the shipped script's behaviour, not a
reimplementation of it. The thresholds, the remind windows and the
resolve-on-recovery handshakes are all things that fail silently in production
if they regress — a watchdog that has stopped alerting looks exactly like a
healthy host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
WATCHDOG = REPO_ROOT / "scripts" / "stand-watchdog.sh"

# The alert key the disk check is expected to use. Asserted on rather than
# ignored, because the key is what ties every message about one condition to a
# single board issue: if it collided with `stand-down`, a filling disk would
# comment on the outage thread instead of opening its own.
DISK_KEY = "disk-pressure"
# Same contract for the dispatch check, and additionally distinct from
# `news-<asset>-<category>`: a broken *bridge* and the alerts it is failing to
# carry are different conditions with different owners, and collapsing them
# would file the infrastructure problem as a comment on a market event.
DISPATCH_KEY = "alert-dispatch-stale"

# The row `docker compose exec db psql ...` returns: stale, errored, oldest-age
# in minutes, sample dispatch_error. Tab-separated because that is what the
# script asks psql for with `-tA -F $'\t'`.
QUIET_ROW = "0\t0\t0\t"


def _fake_repo(tmp_path: Path, pct: int) -> tuple[Path, Path, dict[str, str]]:
    """Lay out a throwaway repo whose `df` reports `pct`, and return its env.

    The script derives its own location to find `notify.sh`, so testing it means
    giving it a directory that looks like a repo rather than overriding a
    variable it does not expose.
    """
    if not WATCHDOG.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/stand-watchdog.sh is not mounted into the test container")

    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(WATCHDOG, scripts / "stand-watchdog.sh")

    # Stub notify.sh: record the full argv of every call as exactly one line.
    #
    # The newlines have to be flattened. A real alert body is a single
    # multi-line argument, so recording it verbatim would spread one call over
    # ~20 lines and a per-line filter would only ever see its first — which is
    # how an assertion on the body's percentage can fail against a script that
    # is in fact emitting it.
    calls = tmp_path / "notify-calls.log"
    notify = scripts / "notify.sh"
    notify.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$(printf "%s" "$*" | tr "\\n" " ")" >> "$NOTIFY_CALLS"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    notify.chmod(0o755)

    # Stub df. One row serves both `df -P` (field 5 = capacity) and `df -h`
    # (fields 2/3/4/5 = size/used/avail/capacity), which is how the real script
    # reads it.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    df = bindir / "df"
    df.write_text(
        "#!/bin/sh\n"
        'echo "Filesystem Size Used Avail Use% Mounted on"\n'
        f'echo "/dev/sda2 32G 23G 7.7G {pct}% /"\n',
        encoding="utf-8",
    )
    df.chmod(0o755)

    # Stub docker, standing in for `docker compose exec -T db psql`. Answers
    # with PSQL_ROW / PSQL_RC / PSQL_STDERR so a test can pose any of the four
    # states the dispatch check distinguishes — quiet, stale, errored,
    # unreadable — without a database. Defaults to the quiet row so the other
    # checks' tests keep exactly one condition in play each.
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "docker %s\\n" "$*" >> "$DOCKER_CALLS"\n'
        '[ -n "${PSQL_STDERR:-}" ] && printf "%s\\n" "$PSQL_STDERR" >&2\n'
        '[ "${PSQL_RC:-0}" -ne 0 ] && exit "$PSQL_RC"\n'
        'printf "%s\\n" "${PSQL_ROW:-}"\n'
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    # A fresh dump so the backup check stays quiet; it shares the notify stub
    # and would otherwise add its own line to the log.
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "audr-20261016.sql.gz.age").write_text("x", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "NOTIFY_CALLS": str(calls),
            "AUDR_WATCHDOG_STATE_DIR": str(tmp_path / "state"),
            "AUDR_BACKUP_DIR": str(backups),
            # The dispatch check `cd`s here before running compose. tmp_path
            # exists, so the stub docker is actually reached — pointing it at a
            # missing directory would make every dispatch test pass through the
            # "cannot read" branch and assert nothing.
            "AUDR_REMOTE_DIR": str(tmp_path),
            "DOCKER_CALLS": str(tmp_path / "docker-calls.log"),
            "PSQL_ROW": QUIET_ROW,
            # Where the spool check (AUD-595) looks. Left un-created on
            # purpose: "no spool file" is the healthy state, so the other
            # checks' tests keep exactly one condition in play each.
            "AUDR_NOTIFY_STATE_DIR": str(tmp_path / "notify-state"),
            # The probe cannot reach a stand from the test container. Port 1 on
            # localhost refuses fast, and a threshold far above one tick keeps
            # that failure from firing an outage alert into the same log.
            "AUDR_PROBE_URL": "http://127.0.0.1:1/health/ready",
            "AUDR_FAIL_THRESHOLD": "9999",
        }
    )
    return scripts / "stand-watchdog.sh", calls, env


def _run(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # S603/S607: the argv is this test's own fixture — a copy of a tracked
    # script under tmp_path — and `bash` is resolved from the test container's
    # PATH exactly as the host unit resolves it. scripts/*.py carry the same two
    # ignores wholesale in ruff.toml; here it stays narrowed to this one call.
    proc = subprocess.run(  # noqa: S603 — argv is the fixture copy of a tracked script
        ["bash", str(script)],  # noqa: S607 — `bash` from PATH, as the host unit runs it
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # The script is written to exit 0 on every condition it is meant to handle;
    # a non-zero exit means it could not do its job at all.
    assert proc.returncode == 0, proc.stderr
    return proc


def _disk_calls(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [
        line
        for line in calls.read_text(encoding="utf-8").splitlines()
        if f"--key {DISK_KEY}" in line
    ]


def test_below_threshold_is_quiet(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["AUDR_DISK_WARN_PCT"] = "85"

    proc = _run(script, env)

    assert _disk_calls(calls) == []
    # The journal line matters: a silent success path makes a dead timer and a
    # healthy disk look identical, which is the wrong failure mode here.
    assert "disk at 70% (limit 85%)" in proc.stdout


def test_at_threshold_alerts(tmp_path: Path) -> None:
    """At, not merely above — 85% with an 85% threshold must fire."""
    script, calls, env = _fake_repo(tmp_path, pct=85)
    env["AUDR_DISK_WARN_PCT"] = "85"

    proc = _run(script, env)

    fired = _disk_calls(calls)
    assert len(fired) == 1
    assert "--resolve" not in fired[0]
    assert "disk is FILLING UP" in fired[0]
    # The percentage has to reach the body; an alert that omits it sends the
    # responder to SSH for the first question.
    assert "85%" in fired[0]
    assert "ALERT sent — disk at 85%" in proc.stdout


def test_remind_window_suppresses_the_second_fire(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=90)
    env["AUDR_DISK_WARN_PCT"] = "85"
    env["AUDR_DISK_REMIND_HOURS"] = "6"

    _run(script, env)
    proc = _run(script, env)

    # Still one alert after two ticks. Without the window this check would send
    # 60 messages an hour and get muted — the failure mode the outage path's
    # REMIND_EVERY exists to avoid.
    assert len(_disk_calls(calls)) == 1
    assert "already alerted" in proc.stdout


def test_recovery_sends_one_resolve_and_clears_state(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=90)
    env["AUDR_DISK_WARN_PCT"] = "85"
    _run(script, env)
    assert len(_disk_calls(calls)) == 1

    # Same state dir, but df now reports a recovered disk.
    recovered, _, env2 = _fake_repo(tmp_path / "after", pct=60)
    env2["AUDR_DISK_WARN_PCT"] = "85"
    env2["AUDR_WATCHDOG_STATE_DIR"] = env["AUDR_WATCHDOG_STATE_DIR"]
    env2["NOTIFY_CALLS"] = str(calls)

    proc = _run(recovered, env2)

    fired = _disk_calls(calls)
    assert len(fired) == 2
    # --resolve is what closes the board issue the alert opened. Without it a
    # recovered host leaves a `critical` issue behind for someone to triage and
    # discover is already fine.
    assert "--resolve" in fired[1]
    assert "cleared" in fired[1]
    assert "stand-down sent" in proc.stdout

    # State cleared, so a later relapse can alert again instead of being
    # swallowed by a stale remind timestamp.
    state = Path(env["AUDR_WATCHDOG_STATE_DIR"]) / "disk-pressure-alerted-at"
    assert not state.exists()

    relapse, _, env3 = _fake_repo(tmp_path / "relapse", pct=95)
    env3["AUDR_DISK_WARN_PCT"] = "85"
    env3["AUDR_WATCHDOG_STATE_DIR"] = env["AUDR_WATCHDOG_STATE_DIR"]
    env3["NOTIFY_CALLS"] = str(calls)
    _run(relapse, env3)
    assert len(_disk_calls(calls)) == 3


def test_quiet_run_sends_no_resolve(tmp_path: Path) -> None:
    """A healthy disk that never alerted must not send a stand-down.

    The recovery branch is gated on the state file for this reason: an
    unconditional resolve would publish a "cleared" message on every tick of a
    host that was never in trouble.
    """
    script, calls, env = _fake_repo(tmp_path, pct=50)
    env["AUDR_DISK_WARN_PCT"] = "85"

    _run(script, env)
    _run(script, env)

    assert _disk_calls(calls) == []


def test_unreadable_disk_does_not_alert(tmp_path: Path) -> None:
    """A df that reports nothing usable is not evidence of a full disk."""
    script, calls, env = _fake_repo(tmp_path, pct=90)
    env["AUDR_DISK_WARN_PCT"] = "85"
    # Shadow the stub with one that produces no parseable row.
    df = Path(env["PATH"].split(":")[0]) / "df"
    df.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    df.chmod(0o755)

    proc = _run(script, env)

    assert _disk_calls(calls) == []
    assert "cannot read disk usage" in proc.stdout


def _dispatch_calls(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [
        line
        for line in calls.read_text(encoding="utf-8").splitlines()
        if f"--key {DISPATCH_KEY}" in line
    ]


def test_no_pending_alerts_is_quiet(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=70)

    proc = _run(script, env)

    assert _dispatch_calls(calls) == []
    # Spoken on purpose: a quiet news day and a check that stopped running must
    # not look identical in the journal.
    assert "alert dispatch ok" in proc.stdout


def test_stale_pending_alert_alerts(tmp_path: Path) -> None:
    """A critical alert pending past the threshold means nothing is delivering."""
    script, calls, env = _fake_repo(tmp_path, pct=70)
    # 2 stale, none errored, oldest 47m — the AUD-584 shape: the dispatcher
    # cannot read the worker at all, so no attempt was ever recorded.
    env["PSQL_ROW"] = "2\t0\t47\t"

    proc = _run(script, env)

    fired = _dispatch_calls(calls)
    assert len(fired) == 1
    assert "--resolve" not in fired[0]
    assert "NOT being dispatched" in fired[0]
    # The counts have to reach the body, and the alert must say to read the
    # journal rather than the unit status — the unit is green in this state,
    # which is the whole reason this check exists.
    assert "oldest: 47m" in fired[0]
    assert "journalctl -u audr-alert-dispatch" in fired[0]
    assert "ALERT sent — alert dispatch stale" in proc.stdout


def test_recorded_dispatch_error_alerts_with_a_different_headline(tmp_path: Path) -> None:
    """A failed attempt and no attempt at all want different first questions."""
    script, calls, env = _fake_repo(tmp_path, pct=70)
    # Errored but NOT yet past the lag threshold: the error path has no grace,
    # because notify.sh exits 0 on every outcome it handles, so a non-zero exit
    # means it could not run at all.
    env["PSQL_ROW"] = "0\t1\t2\tnotify.sh exit 127: not found"

    proc = _run(script, env)

    fired = _dispatch_calls(calls)
    assert len(fired) == 1
    assert "FAILING to dispatch" in fired[0]
    # The recorded error is the single most useful line in the alert; losing it
    # sends the responder to psql for the first question.
    assert "notify.sh exit 127: not found" in fired[0]
    assert "ALERT sent" in proc.stdout


def test_remind_window_suppresses_the_second_dispatch_fire(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["PSQL_ROW"] = "1\t0\t30\t"
    env["AUDR_DISPATCH_REMIND_HOURS"] = "6"

    _run(script, env)
    proc = _run(script, env)

    assert len(_dispatch_calls(calls)) == 1
    assert "already alerted" in proc.stdout


def test_dispatch_recovery_sends_one_resolve_and_clears_state(tmp_path: Path) -> None:
    """The condition clearing must close the board issue it opened.

    This is the second half of AUD-592's acceptance: alert on a prepared overdue
    row, then go quiet once the row is gone.
    """
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["PSQL_ROW"] = "1\t0\t30\t"
    _run(script, env)
    assert len(_dispatch_calls(calls)) == 1

    env["PSQL_ROW"] = QUIET_ROW
    proc = _run(script, env)

    fired = _dispatch_calls(calls)
    assert len(fired) == 2
    assert "--resolve" in fired[1]
    assert "flowing again" in fired[1]
    assert "alert dispatch recovered" in proc.stdout

    state = Path(env["AUDR_WATCHDOG_STATE_DIR"]) / "alert-dispatch-alerted-at"
    assert not state.exists()

    # And a relapse can alert again rather than being swallowed by the stale
    # remind timestamp.
    env["PSQL_ROW"] = "1\t0\t30\t"
    _run(script, env)
    assert len(_dispatch_calls(calls)) == 3


def test_quiet_dispatch_run_sends_no_resolve(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=70)

    _run(script, env)
    _run(script, env)

    assert _dispatch_calls(calls) == []


def test_unreadable_database_does_not_alert(tmp_path: Path) -> None:
    """A stack that cannot be queried is not evidence of a stalled dispatcher.

    Same rule as the disk check's unreadable-df case. Every cause already has an
    owner — a down stand is this script's own outage alert — and guessing in
    either direction is worse than one honest journal line.
    """
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["PSQL_RC"] = "1"
    env["PSQL_STDERR"] = "error: service db is not running"

    proc = _run(script, env)

    assert _dispatch_calls(calls) == []
    assert "cannot read news_alert (exit 1)" in proc.stdout


def test_missing_table_is_named_explicitly(tmp_path: Path) -> None:
    """Before migration 0033 reaches a stand, say so rather than "cannot read".

    Both outcomes are silence, so this is only a journal line — but it is the
    line that distinguishes "phase 1 has not deployed here" from "the database
    is unreachable", which are different problems with different owners.
    """
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["PSQL_RC"] = "3"
    env["PSQL_STDERR"] = 'ERROR:  relation "news_alert" does not exist'

    proc = _run(script, env)

    assert _dispatch_calls(calls) == []
    assert "migration 0033 not applied" in proc.stdout


def test_garbage_row_does_not_alert(tmp_path: Path) -> None:
    """psql exiting 0 with an unparseable row must not be read as a count.

    ON_ERROR_STOP is what normally prevents this, but a changed query or a
    future psql could still hand back something unexpected, and `[ x -ne 0 ]`
    on a non-number is a bash error that `|| true` would swallow into silence.
    """
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["PSQL_ROW"] = "ERROR\tnope\t-\t"

    proc = _run(script, env)

    assert _dispatch_calls(calls) == []
    assert "unexpected row from news_alert query" in proc.stdout


def test_dispatch_query_reads_the_database_not_the_worker(tmp_path: Path) -> None:
    """The read path must not be the dispatcher's own.

    A check that went through `docker compose exec worker python -m
    audr.jobs.alerts_pending` would have been blind to the actual AUD-584
    outage, whose cause was that module being absent from the running image.
    Asserting on the argv pins the independence that makes this check worth
    having at all.
    """
    script, _, env = _fake_repo(tmp_path, pct=70)

    _run(script, env)

    argv = Path(env["DOCKER_CALLS"]).read_text(encoding="utf-8")
    assert "psql" in argv
    assert "exec -T db" in argv
    assert "alerts_pending" not in argv
    assert "worker" not in argv
    # The SQL itself is deliberately absent from argv: psql only interpolates
    # `:'lag'` in its own lexer, so the query goes in on stdin. With -c the
    # server gets the colon unparsed and every tick fails on `syntax error at
    # or near ":"` — measured, and the reason the pipe is there.
    assert "ON_ERROR_STOP=1" in argv
    assert "select count(*)" not in argv
    # A wedged exec must not hold this tick into the next one.
    assert "timeout" in Path(script).read_text(encoding="utf-8")


def test_threshold_default_is_above_host_gc_warn_pct() -> None:
    """The default must stay above host-gc's own 80% escalation point.

    host-gc escalates to 6h retention windows at 80% and usually recovers the
    space by itself, so an alert at or below 80% would fire on every busy merge
    day — the measured 2026-10-16 case, where / hit 75% purely on deploy rate
    with GC working correctly. Pinning the relationship here rather than the
    literal keeps the two files' thresholds from silently crossing.
    """
    if not WATCHDOG.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/stand-watchdog.sh is not mounted into the test container")
    gc_script = REPO_ROOT / "scripts" / "host-gc.sh"
    if not gc_script.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/host-gc.sh is not mounted into the test container")

    def _default(text: str, var: str) -> int:
        for line in text.splitlines():
            if line.startswith(f"{var}="):
                return int(line.split(":-", 1)[1].rstrip('}"'))
        raise AssertionError(f"{var} default not found")

    warn_pct = _default(gc_script.read_text(encoding="utf-8"), "WARN_PCT")
    disk_warn = _default(WATCHDOG.read_text(encoding="utf-8"), "DISK_WARN_PCT")

    assert disk_warn > warn_pct, (
        f"watchdog alerts at {disk_warn}% but host-gc only escalates at "
        f"{warn_pct}% — the alert would fire before GC has had its chance"
    )


# ------------------------------------------------- undrained spool (AUD-595)
#
# The road the dispatch-lag check above deliberately cannot see. notify.sh
# exits 0 when no sink took an alert — it parks it in undelivered.jsonl for
# scripts/drain-alert-spool.py to replay — so the row is marked dispatched and
# leaves the pending set. Correct, and it must stay correct: with the stand's
# board token answering 401, holding those rows pending would keep every alert
# pending forever and make the lag check fire every tick. So the artefact for
# this road is the spool file's own age, the same shape as AUD-567's backup
# check watching the dump's mtime rather than the timer.

SPOOL_KEY = "alert-spool-stale"


def _spool_calls(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [
        line
        for line in calls.read_text(encoding="utf-8").splitlines()
        if f"--key {SPOOL_KEY}" in line
    ]


def _write_spool(env: dict[str, str], records: list[tuple[float, str]]) -> Path:
    """Write undelivered.jsonl from (age-in-hours, key) pairs."""
    spool_dir = Path(env["AUDR_NOTIFY_STATE_DIR"])
    spool_dir.mkdir(parents=True, exist_ok=True)
    path = spool_dir / "undelivered.jsonl"
    now = time.time()
    path.write_text(
        "".join(
            json.dumps(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_h * 3600)),
                    "key": key,
                    "resolve": False,
                    "reason": "unconfigured",
                    "message": "🔴 audr: hack_exploit alert on ETH\nwith a newline and a 'quote'",
                }
            )
            + "\n"
            for age_h, key in records
        ),
        encoding="utf-8",
    )
    return path


def test_no_spool_file_is_quiet_but_not_silent(tmp_path: Path) -> None:
    # A drained spool and a check that stopped running must not look identical.
    script, calls, env = _fake_repo(tmp_path, pct=70)
    proc = _run(script, env)
    assert _spool_calls(calls) == []
    assert "alert spool ok (no spool file" in proc.stdout


def test_a_fresh_spool_does_not_alert(tmp_path: Path) -> None:
    # The drain routine runs every ~3h; an alert spooled an hour ago is simply
    # in flight, not undelivered.
    script, calls, env = _fake_repo(tmp_path, pct=70)
    _write_spool(env, [(1, "news-eth-hack-exploit")])
    proc = _run(script, env)
    assert _spool_calls(calls) == []
    assert "alert spool ok (1 record(s), oldest 1h" in proc.stdout


def test_a_spool_older_than_the_limit_alerts(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["AUDR_SPOOL_MAX_AGE_HOURS"] = "12"
    _write_spool(env, [(20, "news-eth-hack-exploit"), (2, "news-crv-depeg")])

    proc = _run(script, env)

    sent = _spool_calls(calls)
    assert len(sent) == 1, sent
    # The oldest record sets the age — a fresh alert landing on top of a
    # week-old one must not reset its clock.
    assert "oldest 20h" in sent[0]
    assert "PARKED ON DISK" in sent[0]
    # What a reader needs first: the drain command, and that the rows already
    # read as dispatched.
    assert "drain-alert-spool.py" in sent[0]
    assert "dispatched_via = 'spool'" in sent[0]
    assert "ALERT sent — alert spool stale (2 record(s), oldest 20h" in proc.stdout


def test_the_checks_own_alert_does_not_sustain_the_condition(tmp_path: Path) -> None:
    """The self-reference that would make this check unresolvable.

    This alert is sent through notify.sh, which on a stand with no working sink
    spools it — into the very file being measured. Counting it would make the
    condition permanently true from its own first message, so records under its
    own key are excluded from the age.
    """
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["AUDR_SPOOL_MAX_AGE_HOURS"] = "12"
    _write_spool(env, [(30, SPOOL_KEY)])

    proc = _run(script, env)

    assert _spool_calls(calls) == []
    assert "alert spool ok (0 record(s)" in proc.stdout


def test_remind_window_suppresses_the_second_spool_fire(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["AUDR_SPOOL_MAX_AGE_HOURS"] = "12"
    env["AUDR_SPOOL_REMIND_HOURS"] = "12"
    _write_spool(env, [(20, "news-eth-hack-exploit")])

    _run(script, env)
    proc = _run(script, env)

    assert len(_spool_calls(calls)) == 1
    assert "already alerted, reminding in" in proc.stdout


def test_a_drained_spool_sends_one_resolve_and_clears_state(tmp_path: Path) -> None:
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["AUDR_SPOOL_MAX_AGE_HOURS"] = "12"
    spool = _write_spool(env, [(20, "news-eth-hack-exploit")])
    _run(script, env)
    assert len(_spool_calls(calls)) == 1

    # What drain-alert-spool.py leaves behind once it has replayed everything.
    spool.unlink()
    proc = _run(script, env)

    sent = _spool_calls(calls)
    assert len(sent) == 2, sent
    assert "--resolve" in sent[1]
    assert "drained" in proc.stdout
    assert not (Path(env["AUDR_WATCHDOG_STATE_DIR"]) / "alert-spool-stale-alerted-at").exists()

    # And the recovery is sent once, not on every tick afterwards.
    proc = _run(script, env)
    assert len(_spool_calls(calls)) == 2
    assert "--resolve" not in proc.stdout


def test_a_truncated_last_line_is_ignored_not_fatal(tmp_path: Path) -> None:
    # A tick can read the file while notify.sh is appending to it. A half-written
    # record is not evidence of anything — the next tick sees it whole — and must
    # not take the whole check down.
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["AUDR_SPOOL_MAX_AGE_HOURS"] = "12"
    spool = _write_spool(env, [(20, "news-eth-hack-exploit")])
    with spool.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-10-16T10:00:00Z", "key": "news-eth-de')

    proc = _run(script, env)

    sent = _spool_calls(calls)
    assert len(sent) == 1, sent
    assert "oldest 20h" in sent[0]
    assert "1 record(s)" in proc.stdout


def test_an_undatable_record_counts_as_old(tmp_path: Path) -> None:
    # A record nobody can date has still not been delivered, so it must not be
    # silently treated as fresh.
    script, calls, env = _fake_repo(tmp_path, pct=70)
    env["AUDR_SPOOL_MAX_AGE_HOURS"] = "12"
    spool_dir = Path(env["AUDR_NOTIFY_STATE_DIR"])
    spool_dir.mkdir(parents=True, exist_ok=True)
    (spool_dir / "undelivered.jsonl").write_text(
        json.dumps({"key": "news-eth-hack-exploit", "message": "no ts at all"}) + "\n",
        encoding="utf-8",
    )

    _run(script, env)

    assert len(_spool_calls(calls)) == 1


def test_spool_limit_default_clears_the_drain_cadence() -> None:
    """The default must stay several drain cycles wide.

    The drain is an agent routine on a ~3h cadence, so a limit at or under it
    would fire on a merely late heartbeat — the failure mode that gets a
    channel muted (AUD-444's own reasoning about the 5-probe threshold).
    """
    if not WATCHDOG.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/stand-watchdog.sh is not mounted into the test container")
    text = WATCHDOG.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("SPOOL_MAX_AGE_H="):
            assert int(line.split(":-", 1)[1].rstrip('}"')) >= 9
            return
    raise AssertionError("SPOOL_MAX_AGE_H default not found")
