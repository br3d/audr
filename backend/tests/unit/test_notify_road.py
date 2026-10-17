"""notify.sh's `--road-file` contract: which road an alert actually took (AUD-595).

`notify.sh` exits 0 whether a sink accepted the alert or nothing would take it
and it was spooled for `scripts/drain-alert-spool.py`. That is deliberate — an
alerting helper that turns "the stand is down" into "the alerting helper also
broke" is worse than none — but it left `scripts/alert-dispatch.sh` stamping
`news_alert.dispatched_at` with the same meaning for both, so a spooled alert
read as a push to a human. `--road-file` is the fix: one word, written on the
way out, that the dispatcher records as `dispatched_via`.

WHAT IS WORTH TESTING HERE. Only the two things a caller depends on:

  * the word, per exit path — `board`/`telegram`/`board+telegram`, `spool`,
    `none`, `dry-run`. A road that names a sink for an alert that was only
    spooled puts a false value in the one artefact this ticket exists to make
    truthful, and `none` is what tells alert-dispatch.sh an alert reached
    nobody at all and must stay pending;
  * the exit code NOT moving. Every pre-existing caller reads it as "is
    alerting itself broken", and with the stand's board token answering 401
    every alert spools — so a spool that exited non-zero would mark the whole
    alerting path as failed on the stand, continuously. That is the one-liner
    AUD-595 rejected, and this is the guard that stops it being reintroduced.

The sinks themselves are stubbed: the board sink is a fake
`scripts/notify-board.py` (notify.sh resolves it next to itself and documents
its exit codes as the contract — 0 delivered, 3 no credentials), and the
telegram sink stays unconfigured, which is a complete configuration by design.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
NOTIFY = REPO_ROOT / "scripts" / "notify.sh"

# notify-board.py's exit codes, as notify.sh's header records them.
BOARD_DELIVERED = 0
BOARD_NO_CREDENTIALS = 3


def _fake_host(tmp_path: Path, *, board_rc: int | None) -> tuple[Path, Path, dict[str, str]]:
    """Return (notify.sh copy, road file path, env).

    `board_rc=None` leaves no notify-board.py at all, which notify.sh reports as
    an unavailable sink — the same end state as a host with no credentials.
    """
    if not NOTIFY.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/notify.sh is not mounted into the test container")

    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(NOTIFY, scripts / "notify.sh")

    if board_rc is not None:
        board = scripts / "notify-board.py"
        board.write_text(f"import sys\nsys.exit({board_rc})\n", encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "AUDR_NOTIFY_STATE_DIR": str(tmp_path / "state"),
            # No telegram credentials and no secrets directory to find any in:
            # the board sink alone is a complete configuration.
            "AUDR_REMOTE_DIR": str(tmp_path / "stack"),
            "AUDR_NOTIFY_ENV": str(tmp_path / "absent.env"),
        }
    )
    env.pop("TELEGRAM_BOT_TOKEN", None)
    env.pop("TELEGRAM_CHAT_ID", None)
    return scripts / "notify.sh", tmp_path / "road", env


def _run(script: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    # S603/S607 as in test_alert_dispatch.py: the argv is this test's own
    # fixture — a copy of a tracked script under tmp_path — and `bash` resolves
    # from PATH exactly as the host callers resolve it.
    return subprocess.run(  # noqa: S603 — argv is the fixture copy of a tracked script
        ["bash", str(script), *args],  # noqa: S607 — `bash` from PATH, as the callers run it
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _road(road_file: Path) -> str:
    return road_file.read_text(encoding="utf-8").strip() if road_file.exists() else ""


def test_a_sink_that_accepted_the_alert_is_named(tmp_path: Path) -> None:
    # The road names the sink, so an operator reading the row knows WHERE the
    # alert went, not merely that it went somewhere.
    script, road_file, env = _fake_host(tmp_path, board_rc=BOARD_DELIVERED)
    proc = _run(script, env, "--road-file", str(road_file), "--key", "probe", "--", "body")
    assert proc.returncode == 0, proc.stderr
    assert _road(road_file) == "board"


def test_an_alert_no_sink_would_take_is_spooled_and_still_exits_zero(tmp_path: Path) -> None:
    """The gap AUD-595 closes, and the compatibility it must not break."""
    script, road_file, env = _fake_host(tmp_path, board_rc=BOARD_NO_CREDENTIALS)
    proc = _run(script, env, "--road-file", str(road_file), "--key", "probe", "--", "body")

    assert _road(road_file) == "spool"
    # The alert is on disk for drain-alert-spool.py, which is what makes the
    # spool a delivery path rather than a loss...
    spool = tmp_path / "state" / "undelivered.jsonl"
    assert spool.exists() and "body" in spool.read_text(encoding="utf-8")
    # ...and the exit code stays 0, because turning this into a failure would
    # fail every alerting path on a stand whose board token is 401.
    assert proc.returncode == 0, proc.stderr


def test_an_alert_that_could_not_even_be_spooled_reports_none(tmp_path: Path) -> None:
    # No sink took it AND the spool write failed: the only outcome where exit 0
    # really does mean the alert is gone. `none` is also the value the file is
    # pre-set to, so a notify.sh that dies mid-flight reports it too — and
    # alert-dispatch.sh turns that road into a dispatch_error with the row left
    # pending, rather than retiring the last record of an unseen alert.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory\n", encoding="utf-8")
    script, road_file, env = _fake_host(tmp_path, board_rc=BOARD_NO_CREDENTIALS)
    env["AUDR_NOTIFY_STATE_DIR"] = str(blocker / "state")

    proc = _run(script, env, "--road-file", str(road_file), "--key", "probe", "--", "body")

    assert _road(road_file) == "none"
    assert proc.returncode == 0, proc.stderr


def test_a_dry_run_reports_no_other_road(tmp_path: Path) -> None:
    # A drill delivers nothing and spools nothing, so no other word can be true
    # of it — and a drill that recorded itself as a delivery is the shape that
    # published the false AUD-570.
    script, road_file, env = _fake_host(tmp_path, board_rc=BOARD_DELIVERED)
    proc = _run(
        script, env, "--dry-run", "--road-file", str(road_file), "--key", "probe", "--", "body"
    )
    assert proc.returncode == 0, proc.stderr
    assert _road(road_file) == "dry-run"
    assert not (tmp_path / "state" / "undelivered.jsonl").exists()


def test_the_road_file_is_optional(tmp_path: Path) -> None:
    # Every caller older than AUD-595 passes no --road-file, and must see
    # exactly the behaviour it saw before: spooled, reported, exit 0.
    script, _road_file, env = _fake_host(tmp_path, board_rc=BOARD_NO_CREDENTIALS)
    proc = _run(script, env, "--key", "probe", "--", "body")
    assert proc.returncode == 0, proc.stderr
    assert "spooled to" in proc.stdout
