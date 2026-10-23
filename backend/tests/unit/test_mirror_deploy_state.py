"""The mirror's bookmark must mean *deployed*, not *mirrored* (AUD-706).

`audr-github-mirror.sh` recorded the GitHub sha it had pushed to Gitea the
instant it pushed it, and then skipped every later tick that saw the same sha.
The deploy is a consequence of that push, so a commit whose tests went red —
`build-deploy` correctly `skipped` by AUD-687's gate — was bookmarked as shipped
and never retried. The stand went on serving its previous image: healthy,
answering `/health/ready` with `"status":"ok"`, and not `main`.

AUD-701 is the worked example. Run #470 lost its test containers to a host
reboot, the gate refused the deploy, and the stand sat on an image a *dispatch*
run had built from a scratch branch. `last-main-sha` said the commit was done.
Recovery was a human clearing the file by hand.

These tests drive the real `ci/gitea-overlay/audr-github-mirror.sh` — mounted
into the test container by compose.test.yaml — against scratch git
repositories standing in for GitHub and Gitea, and a stubbed `notify.sh` that
records its arguments. That matters more here than usual: the bug was a
one-line ordering mistake in a script that had never executed anywhere but the
live host, where its only witness was the journal. Nothing asserted on below is
a reimplementation of the logic; it is all the shipped source.

What must not regress:

* a commit that never deployed is retried, not forgotten;
* the retry is *bounded*, because a genuinely red commit would otherwise
  rebuild Gitea main every 15 minutes forever;
* reaching the bound raises exactly one board alert, under one `--key`, and
  then goes quiet;
* the alert resolves itself once main is live again, so the channel needs no
  manual cleanup (the failure mode AUD-444 exists to avoid);
* a tag or dispatch deploy never registers as having shipped main — that
  specific confusion is what let AUD-701 run unnoticed;
* a deploy that is merely still *running* is not read as one that failed. The
  timer ticks every 15 minutes and this pipeline has measured 3 to 29 minutes on
  the runner, so that case is the common one for a healthy deploy, not an edge
  case — and getting it wrong would re-mirror a commit that was busy succeeding
  and then alert about it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
MIRROR = REPO_ROOT / "ci" / "gitea-overlay" / "audr-github-mirror.sh"
DEPLOY_WORKFLOW = REPO_ROOT / "ci" / "gitea-overlay" / "workflows" / "deploy.yaml"

# The alert key naming the condition. Asserted on rather than ignored: the key
# is what ties every message about one condition to a single board issue, and a
# collision with `deploy-failed` or `stand-down` would file "main never
# deployed" as a comment on an unrelated thread.
ALERT_KEY = "main-undeployed"

requires_mirror = pytest.mark.skipif(
    not MIRROR.is_file(),
    reason=f"{MIRROR} is not mounted into the test container",
)


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }
    out = subprocess.run(  # noqa: S603 — argv is this test's own scratch repo
        ["git", "-C", str(repo), *args],  # noqa: S607 — `git` from PATH, as the mirror runs it
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return out.stdout.strip()


class Harness:
    """A whole mirror world on disk: a fake GitHub, a fake Gitea, a stub notify."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.cfg = root / "cfg"
        self.cfg.mkdir(parents=True)
        (self.cfg / "overlay").mkdir()

        # "GitHub": a normal repo whose main the mirror fetches.
        self.github = root / "github"
        self.github.mkdir()
        _git(self.github, "init", "-q", "-b", "main")
        (self.github / "VERSION").write_text("0.0.1\n")
        _git(self.github, "add", "-A")
        _git(self.github, "commit", "-qm", "initial")

        # "Gitea": the bare repo the mirror pushes main into.
        self.gitea = root / "gitea.git"
        _git(root, "init", "-q", "--bare", str(self.gitea))
        (self.cfg / "gitea-push-url").write_text(f"{self.gitea}\n")

        self.mirror_dir = root / "mirror.git"
        self.notify_log = root / "notify.log"
        self.notify = root / "notify-stub.sh"
        self.notify.write_text(
            "#!/usr/bin/env bash\n"
            # Record argv one-per-line and the body on stdin, so the test can
            # assert on the key, the --resolve flag and the message separately.
            f'{{ printf "CALL\\n"; for a in "$@"; do printf "%s\\n" "$a"; done; }}'
            f' >> "{self.notify_log}"\n'
            "exit 0\n"
        )
        self.notify.chmod(0o755)

    def env(self, **extra: str) -> dict[str, str]:
        return {
            **os.environ,
            "AUDR_MIRROR_CFG": str(self.cfg),
            "AUDR_MIRROR_DIR": str(self.mirror_dir),
            "AUDR_MIRROR_GH_URL": str(self.github),
            "AUDR_NOTIFY_SH": str(self.notify),
            **extra,
        }

    def run(self, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 — argv is the tracked mirror script
            ["bash", str(MIRROR), *args],  # noqa: S607 — `bash` from PATH, as the host unit runs it
            capture_output=True,
            text=True,
            env=self.env(**extra),
        )

    def decide(self, gh_main: str, **extra: str) -> str:
        proc = self.run("--decide", AUDR_MIRROR_GH_MAIN=gh_main, **extra)
        assert proc.returncode == 0, proc.stderr
        return proc.stdout.strip()

    # --- state helpers -----------------------------------------------------

    def write(self, name: str, value: str) -> None:
        (self.cfg / name).write_text(f"{value}\n")

    def read(self, name: str) -> str | None:
        path = self.cfg / name
        return path.read_text().strip() if path.exists() else None

    def commit_to_github(self, message: str) -> str:
        (self.github / "VERSION").write_text(f"{message}\n")
        _git(self.github, "add", "-A")
        _git(self.github, "commit", "-qm", message)
        return _git(self.github, "rev-parse", "HEAD")

    def gitea_main(self) -> str:
        return _git(self.gitea, "rev-parse", "refs/heads/main")

    def notify_calls(self) -> list[list[str]]:
        if not self.notify_log.exists():
            return []
        calls: list[list[str]] = []
        for line in self.notify_log.read_text().splitlines():
            if line == "CALL":
                calls.append([])
            elif calls:
                calls[-1].append(line)
        return calls

    def age_mirror(self, seconds: int) -> None:
        """Pretend the last mirror happened `seconds` ago."""
        stamp = int((self.cfg / "mirrored-at").read_text().strip()) - seconds
        self.write("mirrored-at", str(stamp))

    def deploy_succeeded(self) -> None:
        """Stand in for deploy.yaml's success-path step."""
        self.write("deployed-gitea-sha", self.gitea_main())


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


SHA_A = "a" * 40
SHA_B = "b" * 40

# Collapse the grace period where a test is about the retry/alert sequence
# rather than about the waiting. The grace period has its own tests above; mixing
# the two concerns would mean every assertion below had to also be an assertion
# about the clock.
NO_GRACE = {"AUDR_MIRROR_DEPLOY_GRACE": "0"}


# ---------------------------------------------------------------------------
# The decision table, driven through --decide with no network at all.
# ---------------------------------------------------------------------------


@requires_mirror
def test_new_github_main_is_mirrored(harness: Harness) -> None:
    # Nothing on record: the ordinary path, unchanged by AUD-706.
    assert harness.decide(SHA_A) == "rebuild"

    harness.write("last-main-sha", SHA_A)
    harness.write("mirrored-gitea-sha", "deadbeef")
    harness.write("deployed-gitea-sha", "deadbeef")
    # A *later* commit still rebuilds even though the previous one is live.
    assert harness.decide(SHA_B) == "rebuild"


@requires_mirror
def test_mirrored_and_deployed_is_quiet(harness: Harness) -> None:
    harness.write("last-main-sha", SHA_A)
    harness.write("mirrored-gitea-sha", "cafe1234")
    harness.write("deployed-gitea-sha", "cafe1234")
    assert harness.decide(SHA_A) == "deployed"


@requires_mirror
def test_a_running_pipeline_is_not_a_failed_one(harness: Harness) -> None:
    """The grace period, and why it is load-bearing rather than a safety margin.

    Ticks are more frequent than deploys are long. Without this branch a tick
    landing mid-deploy re-mirrors the commit, which moves Gitea main, which
    makes the in-flight run record a sha that no longer matches — so the tick
    after that alerts about a deploy that actually worked.
    """
    harness.commit_to_github("second")
    assert harness.run().returncode == 0
    gh = harness.read("last-main-sha") or ""

    # Just mirrored, deploy still running: the exact state of the host one
    # minute after a merge.
    assert harness.decide(gh) == "in-flight"

    # Several ticks later, still inside the grace period.
    harness.age_mirror(1800)
    assert harness.decide(gh) == "in-flight"
    before = harness.gitea_main()
    assert harness.run().returncode == 0
    assert harness.gitea_main() == before, "an in-flight tick must not re-mirror"
    assert harness.notify_calls() == []

    # Past it, with nothing deployed: now it is a failure.
    harness.age_mirror(3600)
    assert harness.decide(gh) == "retry"


@requires_mirror
def test_grace_period_restarts_on_each_retry(harness: Harness) -> None:
    """A retry gets a full pipeline's worth of time, not the remainder of one."""
    harness.commit_to_github("second")
    assert harness.run().returncode == 0
    gh = harness.read("last-main-sha") or ""

    harness.age_mirror(7200)
    assert harness.decide(gh) == "retry"
    assert harness.run().returncode == 0  # performs the retry

    # The retry re-stamped mirrored-at, so the budget is spent but the clock is
    # not: alerting here would be alerting about a pipeline that just started.
    assert harness.read("undeployed-state") == f"{gh} 1 0"
    assert harness.decide(gh) == "in-flight"
    harness.age_mirror(7200)
    assert harness.decide(gh) == "alert"


@requires_mirror
def test_undeployed_commit_is_retried_then_alerted_then_quiet(harness: Harness) -> None:
    """The core of the fix: mirrored != deployed is not 'nothing to do'."""
    harness.write("last-main-sha", SHA_A)
    harness.write("mirrored-gitea-sha", "cafe1234")
    # A different commit is live — this is the AUD-701 shape, where the sha on
    # the stand came from a dispatch build of a scratch branch.
    harness.write("deployed-gitea-sha", "0ldimage")
    harness.write("mirrored-at", "0")  # mirrored long ago; past any grace period

    assert harness.decide(SHA_A) == "retry"

    # Retry spent.
    harness.write("undeployed-state", f"{SHA_A} 1 0")
    assert harness.decide(SHA_A) == "alert"

    # Alert raised. The condition persists, but the mirror must not keep
    # rebuilding and must not keep alerting.
    harness.write("undeployed-state", f"{SHA_A} 1 1")
    assert harness.decide(SHA_A) == "waiting"


@requires_mirror
def test_retry_budget_is_per_commit(harness: Harness) -> None:
    """A spent budget must not disarm the mechanism for the next commit."""
    harness.write("last-main-sha", SHA_A)
    harness.write("mirrored-gitea-sha", "cafe1234")
    harness.write("deployed-gitea-sha", "0ldimage")
    harness.write("mirrored-at", "0")  # mirrored long ago; past any grace period
    # Counter left over from an earlier, different commit.
    harness.write("undeployed-state", f"{SHA_B} 9 1")
    assert harness.decide(SHA_A) == "retry"


@requires_mirror
def test_zero_retries_alerts_immediately(harness: Harness) -> None:
    harness.write("last-main-sha", SHA_A)
    harness.write("mirrored-gitea-sha", "cafe1234")
    harness.write("deployed-gitea-sha", "0ldimage")
    harness.write("mirrored-at", "0")  # mirrored long ago; past any grace period
    assert harness.decide(SHA_A, AUDR_MIRROR_DEPLOY_RETRIES="0") == "alert"


@requires_mirror
def test_a_sha_with_no_timestamp_is_also_treated_as_unknown(harness: Harness) -> None:
    """Half-written state, from a host upgraded between the two versions.

    Guessing "mirrored long ago" would retry a commit that may be mid-deploy —
    the precise thing the grace period exists to prevent. Sync mode stamps it so
    this self-heals on the first tick rather than waiting for the next merge.
    """
    harness.write("last-main-sha", SHA_A)
    harness.write("mirrored-gitea-sha", "cafe1234")
    harness.write("deployed-gitea-sha", "0ldimage")
    assert harness.decide(SHA_A) == "bootstrap"

    harness.commit_to_github("second")
    assert harness.run().returncode == 0
    assert harness.read("mirrored-at") is not None


@requires_mirror
def test_pre_aud706_state_is_neither_retried_nor_alerted(harness: Harness) -> None:
    """Upgrading onto a host whose state file predates the mechanism.

    `mirrored-gitea-sha` was never written, so there is nothing to compare. A
    retry would rebuild a commit that is very likely already live, and an alert
    would be a false alarm on nothing but a missing file. Both are worse than
    waiting for the next real commit, which records it.
    """
    harness.write("last-main-sha", SHA_A)
    assert harness.decide(SHA_A) == "bootstrap"


# ---------------------------------------------------------------------------
# The real sync, against scratch repositories.
# ---------------------------------------------------------------------------


@requires_mirror
def test_sync_records_both_shas(harness: Harness) -> None:
    gh = harness.commit_to_github("second")
    proc = harness.run()
    assert proc.returncode == 0, proc.stderr

    assert harness.read("last-main-sha") == gh
    # The new bookmark: which Gitea commit carries that GitHub sha. Without it
    # the undeployed check has nothing to compare and degrades to the old
    # "mirrored means shipped" behaviour.
    assert harness.read("mirrored-gitea-sha") == harness.gitea_main()
    assert harness.read("undeployed-state") == f"{gh} 0 0"
    # Nothing to alert about on a fresh mirror.
    assert harness.notify_calls() == []


@requires_mirror
def test_undeployed_main_is_rebuilt_once_then_alerted_once(harness: Harness) -> None:
    """End to end: the exact sequence that would have recovered AUD-701 alone."""
    gh = harness.commit_to_github("second")

    assert harness.run(**NO_GRACE).returncode == 0
    first_gitea = harness.gitea_main()

    # The deploy never succeeded for this commit — build-deploy was skipped.
    # Tick two must notice and re-fire CI with a fresh Gitea commit.
    assert harness.run(**NO_GRACE).returncode == 0
    retried_gitea = harness.gitea_main()
    assert retried_gitea != first_gitea, "the retry must mint a new commit, or CI never re-fires"
    assert harness.read("mirrored-gitea-sha") == retried_gitea
    assert harness.read("undeployed-state") == f"{gh} 1 0"
    assert harness.notify_calls() == [], "a retry is not yet news"

    # Tick three: budget spent, raise the alert.
    assert harness.run(**NO_GRACE).returncode == 0
    assert harness.gitea_main() == retried_gitea, "the alert tick must not rebuild again"
    calls = harness.notify_calls()
    assert len(calls) == 1, calls
    assert calls[0][:2] == ["--key", ALERT_KEY]
    assert "--resolve" not in calls[0]

    # Tick four and five: the condition persists and must stay silent. A
    # channel that repeats itself every 15 minutes is a channel that gets muted.
    assert harness.run(**NO_GRACE).returncode == 0
    assert harness.run(**NO_GRACE).returncode == 0
    assert harness.gitea_main() == retried_gitea
    assert len(harness.notify_calls()) == 1


@requires_mirror
def test_alert_resolves_itself_once_main_deploys(harness: Harness) -> None:
    harness.commit_to_github("second")
    harness.run(AUDR_MIRROR_DEPLOY_RETRIES="0", **NO_GRACE)  # mirror
    harness.run(AUDR_MIRROR_DEPLOY_RETRIES="0", **NO_GRACE)  # alert, no budget
    assert len(harness.notify_calls()) == 1

    # Someone fixes the deploy and it finally succeeds for this same commit.
    harness.deploy_succeeded()
    assert harness.run(AUDR_MIRROR_DEPLOY_RETRIES="0", **NO_GRACE).returncode == 0

    calls = harness.notify_calls()
    assert len(calls) == 2, calls
    assert calls[1][:2] == ["--key", ALERT_KEY]
    assert "--resolve" in calls[1], "the board issue must close itself, not wait for a human"

    # And it resolves once, not on every subsequent tick.
    assert harness.run(AUDR_MIRROR_DEPLOY_RETRIES="0", **NO_GRACE).returncode == 0
    assert len(harness.notify_calls()) == 2


@requires_mirror
def test_a_foreign_deployed_sha_does_not_count_as_main(harness: Harness) -> None:
    """A dispatch or tag deploy must never read as 'main shipped'.

    This is the AUD-701 trap in one assertion: the stand was healthy, serving
    `0.3.0-ga0909591f4de` built by a *dispatch* run off the `aud687-gate-probe`
    scratch branch, while main had never deployed. Any scheme that recorded
    "the last thing deployed" without tying it to the mirrored main commit
    would have called that state fine.
    """
    harness.commit_to_github("second")
    assert harness.run().returncode == 0

    harness.write("deployed-gitea-sha", "f" * 40)  # a scratch-branch build
    assert harness.decide(harness.read("last-main-sha") or "", **NO_GRACE) == "retry"


# ---------------------------------------------------------------------------
# The other half of the handshake lives in the workflow, which no unit test can
# execute. Assert its contract textually: the mechanism is only as good as the
# single step that writes the file, and that step is easy to lose in a rebase.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not DEPLOY_WORKFLOW.is_file(),
    reason=f"{DEPLOY_WORKFLOW} is not mounted into the test container",
)
def test_deploy_workflow_records_the_deployed_sha() -> None:
    text = DEPLOY_WORKFLOW.read_text()

    assert "deployed-gitea-sha" in text, (
        "deploy.yaml no longer writes the mirror's deployed-sha handshake; "
        "without it the mirror cannot tell a shipped commit from a skipped one "
        "and AUD-706 regresses silently"
    )

    step = text.split("Record deployed sha for the mirror", 1)
    assert len(step) == 2, "the recording step was renamed or removed"
    body = step[1][:900]

    # Both guards, for different reasons. `success()` keeps a rolled-back
    # deploy from claiming the commit; the ref check keeps a `v*` tag or a
    # dispatch build of a scratch branch from claiming main — which is the
    # state the stand was actually in during AUD-701.
    assert "success()" in body
    assert "refs/heads/main" in body
