"""`promote-image.py`'s staleness guard — the check the other three could not make.

`scripts/promote-image.py` already proved, three ways, that the image it is
about to pin *is* the release tag it claims to be: the manifest resolves, its
`org.opencontainers.image.revision` equals the `v<version>` commit, and that
commit's tree is what gates each healthcheck revert. All three are questions
about provenance, and all three pass for a tag that is six months old.

Promotion is not a provenance decision, though. It writes `x-backend-image`,
which is what every future `git clone && docker compose up -d` pulls — so it
decides *which backend users install*. On 2026-10-18 the pending next step was
"publish v0.1.2, then promote", with v0.1.2 cut from a commit 106 behind `main`:
a genuine image whose promotion would have pinned fresh clones to a seven-day-old
backend missing the AUD-597 migration-rollback fix and the AUD-595 spool fixes.
That was caught by reading, and refused by hand. The `workflow_dispatch` gate
means cut-now-publish-later is the normal shape of a release here, so it will
come round again; these tests are what makes the refusal automatic.

They live in the backend suite rather than next to the script because
`scripts/test_*.py` was run by no CI job (`backend/pyproject.toml` pins
`testpaths = ["tests"]`), and an unrun test reads as coverage while providing
none. AUD-698 moved the stragglers here for that reason — including
test_promote_image.py, four of whose cases turned out to have been failing
unnoticed against a `compose.yaml` that had changed under them.

compose.test.yaml already mounts `scripts/promote-image.py` for
`test_healthcheck.py`, which reads it as text; this file executes it.

Nothing here reaches GHCR: the registry and tag lookups are stubbed, and the
git-backed cases build a throwaway repository in `tmp_path`.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "promote-image.py"

if not _SCRIPT.exists():  # pragma: no cover - mount missing
    pytest.skip(
        "scripts/promote-image.py is not mounted into the test container",
        allow_module_level=True,
    )

_SPEC = importlib.util.spec_from_file_location("promote_image_drift", _SCRIPT)
assert _SPEC and _SPEC.loader
pi = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pi)

REF = "refs/remotes/origin/main"


# --- the decision itself ----------------------------------------------------


def test_current_release_is_promotable() -> None:
    """The tag `main` sits on. The common case must not need a flag."""
    pi.assess_drift("0.1.3", REF, ancestor=True, behind=0)


def test_a_few_commits_during_the_dispatch_wait_are_fine() -> None:
    """The healthy flow: things merge while the publish click is pending.

    If this started refusing, every release would need --allow-stale and the
    flag would become the habit — which is the same as having no guard.
    """
    pi.assess_drift("0.1.3", REF, ancestor=True, behind=3)


def test_the_limit_itself_is_allowed() -> None:
    pi.assess_drift("0.1.3", REF, ancestor=True, behind=pi.MAX_DRIFT)


def test_one_past_the_limit_is_refused() -> None:
    with pytest.raises(pi.Refused):
        pi.assess_drift("0.1.3", REF, ancestor=True, behind=pi.MAX_DRIFT + 1)


def test_the_aud442_near_miss_is_refused() -> None:
    """The measurement that prompted this guard: v0.1.2, 106 commits behind."""
    with pytest.raises(pi.Refused) as caught:
        pi.assess_drift("0.1.2", REF, ancestor=True, behind=106)
    message = str(caught.value)
    assert "106 commits behind" in message
    # A refusal an operator cannot act on just moves the stall, so the remedy
    # has to be in the message: cut a fresh release, don't promote this one.
    assert "./scripts/release.sh patch --push" in message
    assert pi.DISPATCH_URL in message
    assert "--allow-stale" in message


def test_a_tag_off_main_is_refused_however_recent() -> None:
    """Zero drift does not help if the tag is not on `main` at all.

    A tag cut on an unmerged branch, or left behind by a rewritten `main`,
    describes code this repository does not ship. `behind=0` is what a naive
    count-only guard would see.
    """
    with pytest.raises(pi.Refused) as caught:
        pi.assess_drift("0.1.3", REF, ancestor=False, behind=0)
    message = str(caught.value)
    assert "not an ancestor" in message
    assert "--allow-stale" in message


# --- the git plumbing behind it ---------------------------------------------

_GIT = shutil.which("git")
needs_git = pytest.mark.skipif(_GIT is None, reason="git is not installed in this image")


def _git(path: Path, *args: str) -> str:
    """Every git call in this file goes through here: one argv, one exemption."""
    return subprocess.run(  # noqa: S603 - fixed argv, paths are pytest's tmp_path
        [str(_GIT), "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "test")
    _git(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


def _commit(path: Path, message: str) -> str:
    _git(path, "commit", "-q", "--allow-empty", "-m", message)
    return _git(path, "rev-parse", "HEAD")


@needs_git
def test_measure_drift_counts_commits_since_the_tag(tmp_path: Path) -> None:
    root = _repo(tmp_path / "repo")
    tagged = _commit(root, "release")
    for n in range(4):
        _commit(root, f"after {n}")

    ancestor, behind = pi.measure_drift(root, tagged, "refs/heads/main")
    assert (ancestor, behind) == (True, 4)


@needs_git
def test_measure_drift_sees_a_tag_that_left_main(tmp_path: Path) -> None:
    """A commit on a side branch: not an ancestor, and `main` has moved on."""
    root = _repo(tmp_path / "repo")
    _git(root, "checkout", "-q", "-b", "side")
    off_main = _commit(root, "never merged")
    _git(root, "checkout", "-q", "main")
    _commit(root, "main moves on")

    ancestor, behind = pi.measure_drift(root, off_main, "refs/heads/main")
    assert ancestor is False
    assert behind == 1


@needs_git
def test_main_ref_prefers_origin_and_falls_back(tmp_path: Path) -> None:
    """`origin/main` is what a fresh clone gets, so it outranks the local branch."""
    root = _repo(tmp_path / "repo")
    assert pi.main_ref(root) == "refs/heads/main"

    head = _git(root, "rev-parse", "HEAD")
    _git(root, "update-ref", "refs/remotes/origin/main", head)
    assert pi.main_ref(root) == "refs/remotes/origin/main"


@needs_git
def test_main_ref_is_none_without_either(tmp_path: Path) -> None:
    """A clone with no `main` reports nothing rather than crashing the promotion.

    `_promote` prints "staleness unchecked" and carries on in that case; the
    guard must not be the reason a release cannot be promoted from an odd
    checkout.
    """
    root = tmp_path / "bare"
    root.mkdir()
    _git(root, "init", "-q", "-b", "trunk")
    assert pi.main_ref(root) is None


# --- wiring: the guard actually runs, and blocks the rewrite ----------------

COMPOSE = """\
x-backend-image: &backend_image ghcr.io/br3d/audr-backend:0.1.1
services:
  api:
    image: *backend_image
"""


def _stub_registry(monkeypatch: pytest.MonkeyPatch, commit: str) -> None:
    """Every collaborator answers "the image is genuine", leaving drift the variable.

    The healthcheck gates are stubbed rather than left to run because they read
    the release commit's tree with `git show`, and the backend test image has no
    git — these cases are about the drift decision, not about what the tag
    contains. The git-backed tests above skip for the same reason; `measure_drift`
    and `main_ref` are exercised there when git is present.
    """
    monkeypatch.setattr(pi, "anonymous_token", lambda: "token")
    monkeypatch.setattr(pi, "release_commit", lambda root, version: commit)
    monkeypatch.setattr(pi, "image_revision", lambda version, token: commit)
    monkeypatch.setattr(pi, "carries_api_healthcheck", lambda root, commit: False)
    monkeypatch.setattr(pi, "carries_worker_healthcheck", lambda root, commit: False)


def test_promote_refuses_a_stale_tag_and_leaves_compose_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(COMPOSE)
    _stub_registry(monkeypatch, "a" * 40)
    monkeypatch.setattr(pi, "main_ref", lambda root: REF)
    monkeypatch.setattr(pi, "measure_drift", lambda root, commit, ref: (True, 106))

    with pytest.raises(pi.Refused):
        pi._promote(tmp_path, "0.1.2", push=False, check=False)

    # The refusal has to land before the rewrite, not after it: a promotion
    # that edits the pin and then complains leaves the tree holding the exact
    # stale pin the guard exists to prevent.
    assert compose.read_text() == COMPOSE


def test_allow_stale_lets_a_deliberate_old_promotion_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(COMPOSE)
    _stub_registry(monkeypatch, "a" * 40)
    monkeypatch.setattr(pi, "main_ref", lambda root: REF)
    monkeypatch.setattr(pi, "measure_drift", lambda root, commit, ref: (False, 106))

    # --check so the assertion is about reaching the rewrite, not about git.
    assert pi._promote(tmp_path, "0.1.2", push=False, check=True, allow_stale=True) == 0
    assert compose.read_text() == COMPOSE


def test_an_already_promoted_release_stays_a_clean_no_op_as_it_ages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drift grows on its own, so the guard must not fail a run with no work.

    Once `0.1.2` is promoted, `compose.yaml` names it and `main` keeps moving.
    Weeks later the same `./scripts/promote-image.py --check` describes the same
    correct state — but the tag is now hundreds of commits back. If the guard ran
    before the nothing-to-do exit, that invocation would start reporting FATAL
    about a promotion that already happened and is not wrong.
    """
    compose = tmp_path / "compose.yaml"
    compose.write_text(COMPOSE.replace("0.1.1", "0.1.2"))
    _stub_registry(monkeypatch, "a" * 40)
    monkeypatch.setattr(pi, "main_ref", lambda root: REF)
    monkeypatch.setattr(
        pi,
        "measure_drift",
        lambda root, commit, ref: pytest.fail("drift must not be measured when nothing changes"),
    )

    assert pi._promote(tmp_path, "0.1.2", push=False, check=False) == 0


def test_promote_proceeds_when_the_release_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must be invisible on a normal release."""
    compose = tmp_path / "compose.yaml"
    compose.write_text(COMPOSE)
    _stub_registry(monkeypatch, "a" * 40)
    monkeypatch.setattr(pi, "main_ref", lambda root: REF)
    monkeypatch.setattr(pi, "measure_drift", lambda root, commit, ref: (True, 2))

    assert pi._promote(tmp_path, "0.1.3", push=False, check=True) == 0
