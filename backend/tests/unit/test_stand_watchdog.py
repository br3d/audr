"""The watchdog's disk-pressure check (AUD-580).

`scripts/host-gc.sh` has always been able to *detect* disk pressure — it has two
escalating WARN paths for it — but it only ever `echo`s, from an `if: always()`
step inside a CI job whose stdout never reaches the journal. Nothing alerted. So
the condition host-gc calls "Docker is no longer the thing filling this disk"
was invisible, on a host whose own GC header notes that exhaustion "takes the
live stack's database volume down with it".

These tests drive the real `scripts/stand-watchdog.sh` (bind-mounted by
compose.test.yaml) with a stubbed `df` on PATH and a stubbed `notify.sh`, so
what is asserted is the shipped script's behaviour, not a reimplementation of
it. The threshold, the remind window and the resolve-on-recovery handshake are
all things that fail silently in production if they regress — a watchdog that
has stopped alerting looks exactly like a healthy host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
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
