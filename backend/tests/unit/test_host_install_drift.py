"""Guards on the host-install drift check (AUD-698).

The deploy host runs PINNED COPIES of the backup scripts, the watchdog and the
alert path — `scripts/install-*.sh` copy them to `~/.local/share/audr-*` rather
than pointing a systemd unit at a git checkout. That is the right call: the only
checkout on that host is the CI runner's, rewritten on every deploy and reset on
a failed one, so code living there would vanish exactly when a bad deploy makes
it necessary.

The price is drift, and drift in *these* files is the quiet kind. They only run
when something has already gone wrong — a backup, a watchdog, the alert path
itself — so a stale copy is never noticed by its own failure. That is the shape
of AUD-443, two hours of nobody-noticing because the host's copies of the CI
workflow files had fallen behind the repo's.

So two things are tested here:

  * the comparison itself, driven against a local directory standing in for the
    host, because "does this correctly spot a changed file" is the one property
    the whole mechanism rests on; and
  * the pairing between `scripts/lib/host-install-manifest.sh` and the
    installers that consume it. The manifest is only a single source of truth
    while the installers actually read it — an installer that goes back to a
    hand-written `scp` list would drift from the check silently, which is this
    ticket's own bug one level up.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "scripts" / "lib" / "host-install-manifest.sh"
DRIFT_SCRIPT = REPO_ROOT / "scripts" / "host-install-drift.sh"
INSTALLERS = {
    "install-backup.sh": "AUDR_BACKUP_FILES",
    "install-watchdog.sh": "AUDR_WATCHDOG_FILES",
    "install-mirror-dropin.sh": "AUDR_MIRROR_FILES",
}
DEPLOY_WORKFLOW = REPO_ROOT / "ci" / "gitea-overlay" / "workflows" / "deploy.yaml"

if not MANIFEST.exists():  # pragma: no cover - mount missing
    pytest.skip(
        "scripts/lib/host-install-manifest.sh is not mounted into the test container; "
        "add it to compose.test.yaml's backend-tests volumes",
        allow_module_level=True,
    )


def _run_drift(
    entries: list[str], root: Path, install: str = "", remote: str = ""
) -> tuple[int, str]:
    """Call `audr_drift_report` with a local shell standing in for ssh.

    The function takes its connection from the caller's `SSH` array, so setting
    `SSH=(bash -c)` makes the "remote" `sha256sum` run here, against a directory
    playing the part of the host. Nothing is mocked below that line: the real
    hashing, the real path matching and the real verdict all execute.
    """
    script = f"""
set -uo pipefail
. '{MANIFEST}'
SSH=(bash -c)
audr_drift_report '{root}' '{install}' '{remote}' {" ".join(f"'{e}'" for e in entries)}
"""
    # S603/S607 as in test_ci_phase_ledger.py: the argv is a script this test
    # composes from a tracked file plus tmp_path fixtures, and `bash` comes from
    # PATH exactly as the installers run it.
    proc = subprocess.run(  # noqa: S603 — argv is a tracked lib plus tmp_path fixtures
        ["bash", "-c", script],  # noqa: S607 — `bash` from PATH, as the installers run it
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_an_identical_file_is_reported_ok(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "thing.sh").write_text("#!/bin/sh\necho one\n")

    host = tmp_path / "host"
    host.mkdir()
    (host / "thing.sh").write_text("#!/bin/sh\necho one\n")

    rc, out = _run_drift([f"scripts/thing.sh|{host}/thing.sh|verbatim"], repo)
    assert rc == 0, out
    assert "ok " in out
    assert "match this checkout" in out


def test_a_changed_byte_is_drift(tmp_path: Path) -> None:
    """The case the whole mechanism exists for.

    One character different in the host's copy — a threshold edited in the repo
    and never re-installed looks exactly like this — and the host is running
    code that is not this commit's.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "thing.sh").write_text("#!/bin/sh\nTHRESHOLD=90\n")

    host = tmp_path / "host"
    host.mkdir()
    (host / "thing.sh").write_text("#!/bin/sh\nTHRESHOLD=80\n")

    rc, out = _run_drift([f"scripts/thing.sh|{host}/thing.sh|verbatim"], repo)
    assert rc == 1, out
    assert "DRIFTED" in out
    # The report must name the repo-side file, not just the host path: the fix is
    # to re-install, and knowing which source changed is how you tell what for.
    assert "scripts/thing.sh" in out


def test_a_file_the_host_never_got_is_reported_absent(tmp_path: Path) -> None:
    """Distinct wording from DRIFTED, because the action differs.

    ABSENT normally means the installer has never run on this host, or ran as a
    different user so the install directory is somewhere else. DRIFTED means it
    ran and has since fallen behind. Collapsing the two would send the reader
    looking for a version skew that is not there.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "thing.sh").write_text("x\n")

    host = tmp_path / "host"
    host.mkdir()

    rc, out = _run_drift([f"scripts/thing.sh|{host}/thing.sh|verbatim"], repo)
    assert rc == 1, out
    assert "ABSENT" in out


def test_a_rendered_unit_is_compared_after_substitution(tmp_path: Path) -> None:
    """A `render` entry must not report drift on a correctly installed host.

    The unit templates carry @INSTALL_DIR@ and @REMOTE_DIR@, which the installer
    substitutes on the way in — so the host's copy legitimately differs from the
    repo's, byte for byte, forever. Comparing them raw would report permanent
    drift on every host, and an alert that always fires is an alert nobody
    reads. This is the regression that would quietly disable the whole check.
    """
    repo = tmp_path / "repo"
    (repo / "units").mkdir(parents=True)
    (repo / "units" / "u.service.in").write_text(
        "[Service]\nExecStart=@INSTALL_DIR@/scripts/go.sh\nWorkingDirectory=@REMOTE_DIR@\n"
    )

    host = tmp_path / "host"
    host.mkdir()
    (host / "u.service").write_text(
        "[Service]\nExecStart=/opt/audr/scripts/go.sh\nWorkingDirectory=/srv/audr\n"
    )

    rc, out = _run_drift(
        [f"units/u.service.in|{host}/u.service|render"],
        repo,
        install="/opt/audr",
        remote="/srv/audr",
    )
    assert rc == 0, out
    assert "ok " in out

    # And it still catches a real change inside a rendered unit.
    (host / "u.service").write_text(
        "[Service]\nExecStart=/opt/audr/scripts/go.sh\nWorkingDirectory=/srv/elsewhere\n"
    )
    rc, out = _run_drift(
        [f"units/u.service.in|{host}/u.service|render"],
        repo,
        install="/opt/audr",
        remote="/srv/audr",
    )
    assert rc == 1, out
    assert "DRIFTED" in out


def test_a_path_is_matched_exactly_not_by_suffix(tmp_path: Path) -> None:
    """`/a/thing.sh` must not be satisfied by a hash line for `/other/a/thing.sh`.

    The remote side returns `<hash>  <path>` lines for every file in one round
    trip, so the match is a lookup in that output. A loose match would let one
    file's hash answer for another's and report `ok` for a file the host does
    not have at that path at all.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "thing.sh").write_text("same\n")

    host = tmp_path / "host"
    (host / "nested").mkdir(parents=True)
    (host / "nested" / "thing.sh").write_text("same\n")

    # The entry points at <host>/thing.sh, which does not exist; only the nested
    # copy does, and it has identical content. A suffix match would say ok.
    rc, out = _run_drift([f"scripts/thing.sh|{host}/thing.sh|verbatim"], repo)
    assert rc == 1, out
    assert "ABSENT" in out


def _manifest_entries() -> list[str]:
    """Every entry from all three arrays, as the installers would see them."""
    script = (
        f". '{MANIFEST}'\n"
        "printf '%s\\n' \"${AUDR_BACKUP_FILES[@]}\" "
        '"${AUDR_WATCHDOG_FILES[@]}" "${AUDR_MIRROR_FILES[@]}"'
    )
    proc = subprocess.run(  # noqa: S603 — argv is the tracked manifest and nothing else
        ["bash", "-c", script],  # noqa: S607 — `bash` from PATH, as the installers run it
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_every_manifest_entry_names_a_file_that_exists() -> None:
    """A manifest naming a deleted file checks nothing and reports success.

    Renames are the realistic way in: `scripts/backup.sh` moving leaves the
    entry pointing at nothing, and a check that cannot find the repo side has no
    basis for a verdict. It is reported as MISSING-IN-REPO at runtime; this
    catches it at merge time instead.
    """
    entries = _manifest_entries()
    assert entries, "the manifest defined no entries at all"

    missing = [e for e in entries if not (REPO_ROOT / e.split("|")[0]).is_file()]
    assert not missing, f"manifest entries whose source file does not exist: {missing}"


def test_every_manifest_entry_declares_a_known_mode() -> None:
    entries = _manifest_entries()
    bad = [e for e in entries if e.split("|")[2] not in {"verbatim", "render"}]
    assert not bad, f"entries with an unknown mode: {bad}"

    # A `.in` template is a template because it needs substitution. Shipping one
    # verbatim would install a unit with @INSTALL_DIR@ still in it, which
    # systemd accepts and then fails to start.
    wrong = [e for e in entries if e.split("|")[0].endswith(".in") and e.split("|")[2] != "render"]
    assert not wrong, f"`.in` templates must be mode=render: {wrong}"


@pytest.mark.parametrize("installer", sorted(INSTALLERS))
def test_each_installer_reads_the_manifest(installer: str) -> None:
    """The manifest is only a single source of truth while it is actually read.

    An installer that reverts to a hand-written `scp` list drifts from the check
    without anything going red — the same failure this ticket is about, one
    level up: the thing that verifies and the thing that acts stop agreeing.
    """
    path = REPO_ROOT / "scripts" / installer
    if not path.exists():  # pragma: no cover - mount missing
        pytest.skip(f"{installer} is not mounted into the test container")
    source = path.read_text()
    assert "lib/host-install-manifest.sh" in source, (
        f"{installer} does not source the manifest; its file list and the drift "
        "check can now disagree"
    )
    assert INSTALLERS[installer] in source, (
        f"{installer} sources the manifest but never uses {INSTALLERS[installer]}"
    )


@pytest.mark.parametrize("installer", sorted(INSTALLERS))
def test_each_installer_offers_check_drift(installer: str) -> None:
    """`--check-drift` is the contract scripts/host-install-drift.sh calls.

    Also the mode a workflow can gate on: the full `--check` is a human-facing
    report that probes alert sinks over the network, which is noise on every
    deploy and would red on an expired token rather than on drift.
    """
    path = REPO_ROOT / "scripts" / installer
    if not path.exists():  # pragma: no cover - mount missing
        pytest.skip(f"{installer} is not mounted into the test container")
    assert "--check-drift" in path.read_text(), (
        f"{installer} has no --check-drift mode, so host-install-drift.sh cannot ask it anything"
    )


@pytest.mark.parametrize("installer", sorted(INSTALLERS))
def test_no_installer_accepts_a_changed_host_key(installer: str) -> None:
    """`StrictHostKeyChecking=no` also accepts a key that CHANGED (AUD-698 item 3).

    `accept-new` learns an unknown host key without prompting — which is what
    these scripts need, since they run unattended — but still refuses a host
    whose key has been replaced. scripts/deploy.sh has always used the stronger
    option; these three were the stragglers, for no recorded reason.
    """
    path = REPO_ROOT / "scripts" / installer
    if not path.exists():  # pragma: no cover - mount missing
        pytest.skip(f"{installer} is not mounted into the test container")
    source = path.read_text()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if "StrictHostKeyChecking=no" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        f"{installer} still disables host-key checking outright: {offenders}. "
        "Use StrictHostKeyChecking=accept-new."
    )


def test_the_aggregator_asks_every_installer() -> None:
    """An installer missing from the list is a host unit nothing checks.

    This is the pairing that decays most easily: adding a fourth installer is
    the moment nobody thinks about this file, and the drift it introduces is
    invisible by construction.
    """
    if not DRIFT_SCRIPT.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/host-install-drift.sh is not mounted into the test container")
    source = DRIFT_SCRIPT.read_text()
    missing = [name for name in INSTALLERS if name not in source]
    assert not missing, (
        f"scripts/host-install-drift.sh never calls: {missing}; those installers' "
        "pinned copies are checked by nothing"
    )


def test_the_deploy_workflow_runs_the_drift_check() -> None:
    """A check nothing schedules is a check that runs the day after it was needed.

    These files only execute when something has already gone wrong, so their
    staleness cannot be noticed by use. Every deploy is the cadence that matters:
    a deploy is when the repo moves, which is when the host starts being behind.
    """
    if not DEPLOY_WORKFLOW.exists():  # pragma: no cover - mount missing
        pytest.skip("ci/gitea-overlay/workflows/deploy.yaml is not mounted into the test container")
    workflow = DEPLOY_WORKFLOW.read_text()
    assert "host-install-drift.sh" in workflow, (
        "the deploy workflow does not run scripts/host-install-drift.sh, so host "
        "drift is measured by nothing"
    )


def test_the_deploy_step_asks_about_the_machine_it_runs_on() -> None:
    """The drift step must pass AUDR_DEPLOY_HOST=local, or it checks nothing.

    The deploy job is `runs-on: host`: it executes on the deploy host, drives
    compose locally and ssh's nowhere, so nothing sets AUDR_DEPLOY_HOST there
    and no key is configured to make a hop with. Without `local` the installers
    cannot name a host, every run exits 2 ("could not check"), and the step goes
    green having measured nothing.

    That is the failure worth a test, because it fails OPEN and silently: a
    green step named "Check host-unit drift" reads as "no drift" to every later
    reader, which is strictly worse than having no step at all. It is also an
    easy thing to drop in a rebase, since it is one `env:` line.
    """
    if not DEPLOY_WORKFLOW.exists():  # pragma: no cover - mount missing
        pytest.skip("ci/gitea-overlay/workflows/deploy.yaml is not mounted into the test container")
    workflow = DEPLOY_WORKFLOW.read_text()
    step = workflow.split("name: Check host-unit drift", 1)
    assert len(step) == 2, "the deploy workflow has no 'Check host-unit drift' step"
    # Within the step's own block, not merely somewhere in the file.
    block = step[1].split("- name:", 1)[0]
    assert "AUDR_DEPLOY_HOST: local" in block, (
        "the host-unit drift step does not set AUDR_DEPLOY_HOST=local, so it will "
        "exit 2 ('could not check') on every deploy and report green having "
        "checked nothing"
    )


def test_the_drift_alert_is_keyed_and_self_resolving() -> None:
    """One issue per condition, not one per deploy.

    Without `--key` the board sink opens a fresh `critical` issue on every
    drifted deploy, and an alert channel that does that gets muted — which costs
    more than the drift. `--resolve` is the other half: re-running the installer
    has to clear the alert without anybody remembering to close it.
    """
    if not DRIFT_SCRIPT.exists():  # pragma: no cover - mount missing
        pytest.skip("scripts/host-install-drift.sh is not mounted into the test container")
    source = DRIFT_SCRIPT.read_text()
    assert "--key" in source, "the drift alert is unkeyed and will open one issue per deploy"
    assert "--resolve" in source, (
        "nothing resolves the drift alert, so a re-installed host keeps a stale critical issue open"
    )
