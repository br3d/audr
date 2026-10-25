"""The scheduled alert-sink probe and its exit-code mapping (AUD-757).

`notify-board.py --probe` has been able to answer "does this credential
deliver?" since AUD-594, and for the whole of its life its only caller was
`install-watchdog.sh` — i.e. install time, the one moment a freshly-pasted
credential is certain to work. AUD-753 is the bill for that: the deploy host
held a run-scoped agent JWT with a 48-hour TTL, it expired, and for NINE DAYS
every alert bought a 401 and fell back to the spool. Nothing was lost, because
notify.sh spools and the drain routine replays — but the host looked configured
the whole time and the slow path was the only path.

So `--check-sinks` asks on a schedule, and the thing under test here is its
three-way verdict:

    0  a sink answered                1  configured and REFUSED
    3  nothing is configured

Every way that mapping can regress is silent, which is why it is pinned:

  * a refused credential read as "unconfigured" goes unalerted — AUD-753 again,
    with a probe in place that says nothing;
  * an unconfigured host read as "refused" opens a `critical` issue on every
    deploy, which is how an alert key gets muted and the next real one ignored;
  * a probe that could not RUN (no AUDR_DEPLOY_HOST, ssh refused) read as either
    makes a claim about a host nobody named — the mistake
    scripts/host-install-drift.sh made on its first run.

Two subjects, driven two ways. `install-watchdog.sh --check-sinks` runs for
real, with AUDR_DEPLOY_HOST=local substituting a local shell for ssh (the same
trick the deploy workflow uses, since that job executes ON the host) and a stub
notify-board.py standing in for the installed copy. alert-sink-probe.sh runs for
real too, inside a scratch directory laid out like the repo, so its notion of
"which verdict means alert" is exercised without a board or a network.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
INSTALLER = REPO_ROOT / "scripts" / "install-watchdog.sh"
PROBE_CALLER = REPO_ROOT / "scripts" / "alert-sink-probe.sh"
DEPLOY_WORKFLOW = REPO_ROOT / "ci" / "gitea-overlay" / "workflows" / "deploy.yaml"

# The key every message about this one condition must carry. Asserted on rather
# than ignored: the key is what ties repeated deploys to a single board issue,
# and what lets a recovered sink close it. A typo here would open one `critical`
# issue per deploy and resolve none of them.
ALERT_KEY = "alert-sink-dead"

if not INSTALLER.exists() or not PROBE_CALLER.exists():  # pragma: no cover - mount missing
    pytest.skip(
        "scripts/install-watchdog.sh and scripts/alert-sink-probe.sh are not mounted "
        "into the test container; add them to compose.test.yaml's backend-tests volumes",
        allow_module_level=True,
    )


# --------------------------------------------------------------- --check-sinks


def _sinks_env(tmp_path: Path, *, board_rc: int | None, telegram: str = "absent") -> dict[str, str]:
    """Lay out a fake host and return the environment to probe it with.

    `board_rc` is the exit code the *installed* notify-board.py will answer
    `--probe` with; None leaves no installed copy at all. `telegram` is one of
    absent / partial / live, the three states the Telegram probe can reach
    without a real token.
    """
    home = tmp_path / "home"
    install = home / "install"
    (install / "scripts").mkdir(parents=True)
    remote = tmp_path / "remote"
    (remote / "secrets").mkdir(parents=True)

    if board_rc is not None:
        # Python, because the installer invokes this path through `python3` —
        # the stub has to be reachable the way the real one is. sys.exit alone,
        # so what is pinned is the installer's reading of the contract rather
        # than a reimplementation of the probe.
        (install / "scripts" / "notify-board.py").write_text(
            f"import sys\nsys.exit({board_rc})\n", encoding="utf-8"
        )

    if telegram == "partial":
        # A token with no chat_id: exits 1 with no network call, which is the
        # cheapest way to pose "this sink is configured and unusable".
        (remote / "secrets" / "telegram.env").write_text(
            "TELEGRAM_BOT_TOKEN=stub-token-never-logged\n", encoding="utf-8"
        )
    elif telegram == "live":
        (remote / "secrets" / "telegram.env").write_text(
            "TELEGRAM_BOT_TOKEN=stub-token-never-logged\nTELEGRAM_CHAT_ID=42\n",
            encoding="utf-8",
        )

    bindir = tmp_path / "bin"
    bindir.mkdir()
    # getMe answers 200 for the `live` case and is never reached otherwise. A
    # stub rather than a network call: the point of the test is the mapping, and
    # a probe that really talked to api.telegram.org would be a flake.
    curl = bindir / "curl"
    curl.write_text("#!/bin/sh\nprintf 200\n", encoding="utf-8")
    curl.chmod(0o755)

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{bindir}:{env['PATH']}",
            # `local` is what the deploy workflow passes, for the same reason:
            # that job runs on the host and has no ssh destination to hop with.
            "AUDR_DEPLOY_HOST": "local",
            # The installer resolves the install directory against the remote
            # $HOME, so the fake host's home is this.
            "HOME": str(home),
            "AUDR_WATCHDOG_DIR": "install",
            "AUDR_REMOTE_DIR": str(remote),
            # Never read a deploy.env that happens to be lying around in the
            # checkout: it could set AUDR_REMOTE_DIR out from under the fixture.
            "AUDR_DEPLOY_ENV": str(tmp_path / "no-such-deploy.env"),
        }
    )
    return env


def _check_sinks(tmp_path: Path, **kwargs: object) -> tuple[int, str]:
    env = _sinks_env(tmp_path, **kwargs)  # type: ignore[arg-type]
    # S603/S607 as in test_host_install_drift.py: the argv is a tracked script
    # plus tmp_path fixtures, and `bash` comes from PATH exactly as the deploy
    # workflow runs it.
    proc = subprocess.run(  # noqa: S603 — argv is a tracked script plus tmp_path fixtures
        ["bash", str(INSTALLER), "--check-sinks"],  # noqa: S607 — `bash` from PATH, as CI runs it
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_a_live_credential_exits_zero(tmp_path: Path) -> None:
    rc, out = _check_sinks(tmp_path, board_rc=0)
    assert rc == 0, out
    assert "SINK VERDICT: live" in out
    assert "board" in out


def test_a_refused_credential_exits_one(tmp_path: Path) -> None:
    """The AUD-753 condition, and the only one that may raise an alert."""
    rc, out = _check_sinks(tmp_path, board_rc=1)
    assert rc == 1, out
    assert "SINK VERDICT: dead" in out
    assert "board" in out


def test_no_credential_at_all_exits_three(tmp_path: Path) -> None:
    """The deploy host's current, intended state — and it must stay quiet.

    Only a human board user can mint the long-lived `task_bridge` key the board
    sink wants, and AUD-591 settled that the host runs on the spool until one
    exists. An alert here would fire on every deploy forever.
    """
    rc, out = _check_sinks(tmp_path, board_rc=3)
    assert rc == 3, out
    assert "SINK VERDICT: unconfigured" in out
    assert "dead" not in out


def test_a_live_telegram_does_not_excuse_a_dead_board(tmp_path: Path) -> None:
    """Worst-wins, deliberately.

    A second working sink makes an expired board key *unnoticed*, which is the
    whole of AUD-753 — it does not make it less dead. So the verdict follows the
    refused sink even though alerts are still being delivered, and it names both
    so the reader is not told the host is broken when it is only half broken.
    """
    rc, out = _check_sinks(tmp_path, board_rc=1, telegram="live")
    assert rc == 1, out
    assert "SINK VERDICT: dead" in out
    assert "refused: board" in out
    assert "still live: telegram" in out


def test_a_broken_telegram_alone_is_dead(tmp_path: Path) -> None:
    """Both sinks are probed, so either one can produce the verdict."""
    rc, out = _check_sinks(tmp_path, board_rc=3, telegram="partial")
    assert rc == 1, out
    assert "SINK VERDICT: dead" in out
    assert "refused: telegram" in out


def test_an_unknown_probe_code_counts_as_dead(tmp_path: Path) -> None:
    """Unknown resolves towards the louder reading.

    An installed notify-board.py that predates `--probe` exits 2 — argparse's
    usage error — which is outside the contract. Reading an unrecognised code as
    "no credentials here" would report a cheerful unconfigured host over a
    channel that exists and is failing.
    """
    rc, out = _check_sinks(tmp_path, board_rc=2)
    assert rc == 1, out
    assert "SINK VERDICT: dead" in out


def test_a_missing_installed_copy_is_unconfigured_not_dead(tmp_path: Path) -> None:
    """Nothing installed is nothing to alert about; the drift check owns that."""
    rc, out = _check_sinks(tmp_path, board_rc=None)
    assert rc == 3, out
    assert "SINK VERDICT: unconfigured" in out


def test_the_probe_sends_nothing_and_logs_no_credential(tmp_path: Path) -> None:
    """Read-only, and quiet about what it read.

    A probe that proved delivery by delivering would leave a `critical` issue
    behind every deploy (AUD-570's mistake with extra steps), and one that
    echoed the token would put it in every run log of a public repo.
    """
    rc, out = _check_sinks(tmp_path, board_rc=0, telegram="live")
    assert rc == 0, out
    assert "nothing sent" in out
    assert "stub-token-never-logged" not in out


# ------------------------------------------------------- alert-sink-probe.sh


def _fake_repo(tmp_path: Path, *, verdict_out: str, verdict_rc: int) -> tuple[Path, Path]:
    """A scratch directory laid out like the repo, with both subjects stubbed.

    alert-sink-probe.sh finds its two collaborators relative to its own path, so
    standing it up here is what lets a test pose any verdict — including the
    ones a real host cannot be made to produce on demand — and see whether an
    alert is raised, resolved, or correctly withheld.
    """
    scripts = tmp_path / "repo" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "alert-sink-probe.sh").write_text(
        PROBE_CALLER.read_text(encoding="utf-8"), encoding="utf-8"
    )

    installer = scripts / "install-watchdog.sh"
    installer.write_text(
        f"#!/bin/sh\ncat <<'OUT'\n{verdict_out}\nOUT\nexit {verdict_rc}\n", encoding="utf-8"
    )
    installer.chmod(0o755)

    calls = tmp_path / "notify-calls.log"
    notify = scripts / "notify.sh"
    # Records argv and nothing else. The body arrives on stdin and is
    # deliberately not logged: the assertions are about WHETHER an alert was
    # raised and under which key, which is what dedupe and self-resolve rest on.
    notify.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{calls}"\nexit 0\n',
        encoding="utf-8",
    )
    notify.chmod(0o755)
    return scripts / "alert-sink-probe.sh", calls


def _run_caller(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 — argv is the fixture copy of a tracked script
        ["bash", str(script), *args],  # noqa: S607 — `bash` from PATH, as the workflow runs it
        capture_output=True,
        text=True,
        timeout=120,
    )


def _calls(calls: Path) -> list[str]:
    if not calls.exists():
        return []
    return [line for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()]


DEAD = "==> SINK VERDICT: dead — configured and refused: board"
LIVE = "==> SINK VERDICT: live — probed: board"
UNCONFIGURED = "==> SINK VERDICT: unconfigured — no sink holds a credential on this host"


def test_a_dead_sink_raises_exactly_one_keyed_alert(tmp_path: Path) -> None:
    script, calls = _fake_repo(tmp_path, verdict_out=DEAD, verdict_rc=1)
    proc = _run_caller(script)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _calls(calls) == [f"--key {ALERT_KEY}"], _calls(calls)


def test_a_live_sink_resolves_the_alert(tmp_path: Path) -> None:
    """The other half of the handshake.

    Without it the `critical` issue outlives the problem: the credential is
    replaced, the sink answers, and the issue sits there until a human notices
    it is stale. An alert channel that needs manual cleanup gets muted, which is
    the failure mode AUD-444 exists to avoid.
    """
    script, calls = _fake_repo(tmp_path, verdict_out=LIVE, verdict_rc=0)
    proc = _run_caller(script)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # One call, and it must carry both the key and --resolve: the key alone
    # would comment "recovered" onto the open issue and leave it open.
    assert len(_calls(calls)) == 1, _calls(calls)
    assert _calls(calls)[0].startswith(f"--key {ALERT_KEY} --resolve"), _calls(calls)


def test_an_unconfigured_host_is_silent_in_both_directions(tmp_path: Path) -> None:
    """No alert — and no resolve either.

    A credential that was dead and has since been DELETED has not recovered, so
    closing the issue would say something untrue about a host that now delivers
    nothing at all.
    """
    script, calls = _fake_repo(tmp_path, verdict_out=UNCONFIGURED, verdict_rc=3)
    proc = _run_caller(script)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    assert _calls(calls) == []


def test_a_probe_that_could_not_run_alerts_nothing(tmp_path: Path) -> None:
    """The false-alert guard.

    `--check-sinks` exits 1 for reasons that are not about a credential at all:
    `audr_require` fails with 1 when AUDR_DEPLOY_HOST is unset, and an ssh that
    cannot connect fails the same way — on any machine that is not the deploy
    host, that is the normal state. The verdict is therefore read off the marker
    line, not the exit code, and its absence is its own outcome.
    """
    script, calls = _fake_repo(
        tmp_path, verdict_out="ERROR: AUDR_DEPLOY_HOST is not set.", verdict_rc=1
    )
    proc = _run_caller(script)
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert _calls(calls) == []
    assert "COULD NOT PROBE" in proc.stdout


def test_dry_run_delivers_nothing(tmp_path: Path) -> None:
    script, calls = _fake_repo(tmp_path, verdict_out=DEAD, verdict_rc=1)
    proc = _run_caller(script, "--dry-run")
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _calls(calls) == []
    assert ALERT_KEY in proc.stdout


# ------------------------------------------------------------- the schedule


@pytest.mark.skipif(not DEPLOY_WORKFLOW.exists(), reason="deploy workflow not mounted")
def test_the_deploy_workflow_still_runs_the_probe() -> None:
    """The caller is the schedule. Without the step, all of the above is inert.

    Pinned the same way test_host_install_drift.py pins the drift step, and for
    the same two reasons: a rebase can drop a single workflow step without
    anything going red, and `AUDR_DEPLOY_HOST: local` is what makes the step
    measure anything at all on a job that runs ON the host with no ssh
    destination set. Losing either fails open and silently.
    """
    text = DEPLOY_WORKFLOW.read_text(encoding="utf-8")
    assert "scripts/alert-sink-probe.sh" in text, (
        "the deploy workflow no longer probes the alert sinks; a dead credential "
        "would go unnoticed again (AUD-753)"
    )
    # The step's own block, not the whole file: the drift step above it also
    # sets AUDR_DEPLOY_HOST, so searching the file would assert nothing.
    step = text.split("- name: Probe alert sinks")[1].split("scripts/alert-sink-probe.sh")[0]
    assert "AUDR_DEPLOY_HOST: local" in step, (
        "the probe step needs AUDR_DEPLOY_HOST=local — the deploy job runs on the "
        "host and has no ssh destination, so without it every run exits 'could not check'"
    )
