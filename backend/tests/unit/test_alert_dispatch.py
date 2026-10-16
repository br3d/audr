"""The host news-alert dispatcher, scripts/alert-dispatch.sh (AUD-584, ADR-007 §1).

Same shape as tests/unit/test_stand_watchdog.py: the real shipped script runs
against stubs — a stub `docker` on PATH standing in for the worker's two CLI
points, and a stub notify.sh — so what is asserted is the script, not a
reimplementation of it.

WHAT IS WORTH TESTING. Not the message text; it is printf. The failure modes
that cost something:

  * marking an alert dispatched when notify.sh did NOT accept it — the row never
    becomes pending again, so the alert is lost silently, which is strictly
    worse than never having built this;
  * not marking one that WAS accepted — a board issue commented on every minute
    forever;
  * an article headline with a quote, a newline or a leading dash breaking the
    command line or being read as a notify.sh flag (headlines are untrusted
    input — ADR-007 §2);
  * a down stack turning into a failed unit every 60s instead of one journal
    line, which is how a noisy journal trains everyone to ignore it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
DISPATCH = REPO_ROOT / "scripts" / "alert-dispatch.sh"

ALERT = {
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


def _fake_host(
    tmp_path: Path,
    *,
    pending: list[dict[str, object]],
    notify_rc: int = 0,
    docker_rc: int = 0,
) -> tuple[Path, Path, dict[str, str]]:
    """Lay out a throwaway host and return (script, call log, env).

    The script finds notify.sh relative to its own location, so testing it means
    giving it a directory that looks like a repo rather than overriding a
    variable it does not expose.
    """
    if not DISPATCH.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/alert-dispatch.sh is not mounted into the test container")

    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(DISPATCH, scripts / "alert-dispatch.sh")

    calls = tmp_path / "calls.log"
    pending_file = tmp_path / "pending.json"
    pending_file.write_text(json.dumps(pending), encoding="utf-8")

    # Stub docker. Records its whole argv per call and answers as the two CLI
    # points do: alerts_pending prints the JSON array, alerts_mark a count line.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    docker = bindir / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "docker %s\\n" "$*" >> "$CALLS"\n'
        f"[ {docker_rc} -ne 0 ] && exit {docker_rc}\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        '    audr.jobs.alerts_pending) cat "$PENDING_JSON"; exit 0 ;;\n'
        "    audr.jobs.alerts_mark) echo '[alerts_mark] ok'; exit 0 ;;\n"
        "  esac\n"
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    docker.chmod(0o755)

    # Stub notify.sh: one line per ARGUMENT, not per call. A body that leaked
    # into the flag position is then visible in the assertion instead of merely
    # plausible — which is the whole point of the hostile-headline test.
    notify = scripts / "notify.sh"
    notify.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  printf "notify-arg %s\\n" "$(printf "%s" "$a" | tr "\\n" " ")" >> "$CALLS"\n'
        "done\n"
        # What notify.sh inherits is as much a part of the contract as its argv:
        # it resolves the board and telegram credentials under
        # $AUDR_REMOTE_DIR/secrets/, so an unset value silently costs a sink.
        'printf "notify-env AUDR_REMOTE_DIR=%s\\n" "${AUDR_REMOTE_DIR:-}" >> "$CALLS"\n'
        'printf "notify-call\\n" >> "$CALLS"\n'
        f"exit {notify_rc}\n",
        encoding="utf-8",
    )
    notify.chmod(0o755)

    stack = tmp_path / "stack"
    stack.mkdir()

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            "CALLS": str(calls),
            "PENDING_JSON": str(pending_file),
            "AUDR_REMOTE_DIR": str(stack),
        }
    )
    return scripts / "alert-dispatch.sh", calls, env


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # S603/S607 as in test_stand_watchdog.py: the argv is this test's own
    # fixture — a copy of a tracked script under tmp_path — and `bash` resolves
    # from PATH exactly as the host unit resolves it.
    proc = subprocess.run(  # noqa: S603 — argv is the fixture copy of a tracked script
        ["bash", str(script), *args],  # noqa: S607 — `bash` from PATH, as the host unit runs it
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # Every condition this script handles exits 0; non-zero means it could not
    # do its job at all.
    assert proc.returncode == 0, proc.stderr
    return proc


def _lines(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return calls.read_text(encoding="utf-8").splitlines()


def _notify_args(calls: Path) -> list[str]:
    return [line.split(" ", 1)[1] for line in _lines(calls) if line.startswith("notify-arg ")]


def _marks(calls: Path) -> list[str]:
    return [line for line in _lines(calls) if "alerts_mark" in line]


def test_quiet_tick_says_so_and_sends_nothing(tmp_path: Path) -> None:
    # A dead timer and a quiet news day must not look identical in the journal.
    script, calls, env = _fake_host(tmp_path, pending=[])
    out = _run(script, env)
    assert "no pending critical alerts" in out.stdout
    assert "notify-call" not in _lines(calls)


def test_delivered_alert_is_marked_dispatched(tmp_path: Path) -> None:
    script, calls, env = _fake_host(tmp_path, pending=[ALERT])
    out = _run(script, env)
    assert "1 pending, 1 dispatched, 0 failed" in out.stdout
    marks = _marks(calls)
    assert len(marks) == 1, _lines(calls)
    assert ALERT["id"] in marks[0]
    assert "--error" not in marks[0]


def test_notify_inherits_a_remote_dir_even_when_the_caller_sets_none(tmp_path: Path) -> None:
    """The credentials directory must survive a call that did not export it.

    `notify.sh` and `notify-board.py` look for `paperclip.env`/`telegram.env`
    under `$AUDR_REMOTE_DIR/secrets/`, falling back to `$REPO_DIR/secrets/` —
    which on the host is the install directory, where no secret ever lives. So
    an unset `AUDR_REMOTE_DIR` does not fail: it reports `no channel
    configured` and spools the alert, a line that looks like a healthy quiet
    tick in the journal. The systemd unit sets the variable, which is why
    production was unaffected and why this went unnoticed — it only showed up
    drilling the script by hand on the stand (AUD-584).
    """
    script, calls, env = _fake_host(tmp_path, pending=[ALERT])
    home = tmp_path / "home"
    (home / "audr").mkdir(parents=True)
    env.pop("AUDR_REMOTE_DIR")
    env["HOME"] = str(home)

    _run(script, env)

    seen = [line for line in _lines(calls) if line.startswith("notify-env ")]
    assert seen == [f"notify-env AUDR_REMOTE_DIR={home / 'audr'}"], _lines(calls)


def test_key_is_passed_and_nothing_resolves(tmp_path: Path) -> None:
    script, calls, env = _fake_host(tmp_path, pending=[ALERT])
    _run(script, env)
    args = _notify_args(calls)
    assert "--key" in args
    assert ALERT["key"] in args
    # ADR-007 §1: a news story has no "incident over" moment, so nothing here
    # may close the board issue it opened — its assignee does that.
    assert "--resolve" not in args


def test_a_refused_alert_is_not_marked_dispatched(tmp_path: Path) -> None:
    """The expensive bug: marking on failure loses the alert permanently."""
    script, calls, env = _fake_host(tmp_path, pending=[ALERT], notify_rc=1)
    out = _run(script, env)
    assert "1 pending, 0 dispatched, 1 failed" in out.stdout
    marks = _marks(calls)
    assert len(marks) == 1, _lines(calls)
    assert "--error" in marks[0]


def test_hostile_headline_cannot_become_a_flag(tmp_path: Path) -> None:
    hostile = dict(ALERT)
    hostile["title"] = '--resolve "quoted" $(touch /tmp/audr-pwned)\nsecond line\ttabbed'
    hostile["summary"] = "-- leading dashes --"
    script, calls, env = _fake_host(tmp_path, pending=[hostile])
    out = _run(script, env)
    assert "1 pending, 1 dispatched, 0 failed" in out.stdout
    args = _notify_args(calls)
    # The flags before `--` are only the ones the script chose, and the body
    # arrives as exactly one argument after it.
    assert args[:3] == ["--key", hostile["key"], "--"]
    assert len(args) == 4, args
    assert not Path("/tmp/audr-pwned").exists(), "command substitution ran"  # noqa: S108
    # Two calls would mean the embedded newline split one alert into two rows.
    assert len([line for line in _lines(calls) if line == "notify-call"]) == 1


def test_a_down_stack_is_one_journal_line(tmp_path: Path) -> None:
    # The outage itself is audr-watchdog's alert to send; a failed unit here
    # every 60s would only repeat it.
    script, calls, env = _fake_host(tmp_path, pending=[ALERT], docker_rc=1)
    out = _run(script, env)
    assert "cannot read pending alerts" in out.stdout
    assert "notify-call" not in _lines(calls)


def test_malformed_worker_output_makes_the_tick_a_no_op(tmp_path: Path) -> None:
    script, calls, env = _fake_host(tmp_path, pending=[])
    Path(env["PENDING_JSON"]).write_text("Traceback (most recent call last):", encoding="utf-8")
    out = _run(script, env)
    assert "did not return the expected JSON" in out.stderr
    assert "notify-call" not in _lines(calls)


def test_dry_run_delivers_nothing_and_marks_nothing(tmp_path: Path) -> None:
    script, calls, env = _fake_host(tmp_path, pending=[ALERT])
    out = _run(script, env, "--dry-run")
    assert "DRY RUN" in out.stdout
    assert "--dry-run" in _notify_args(calls)
    assert _marks(calls) == []


def test_pending_is_bounded_per_tick(tmp_path: Path) -> None:
    # A burst must not become fifty board issues in one minute.
    script, calls, env = _fake_host(tmp_path, pending=[])
    _run(script, env)
    pending_calls = [line for line in _lines(calls) if "alerts_pending" in line]
    assert len(pending_calls) == 1, _lines(calls)
    assert "--limit 20" in pending_calls[0]
