"""The base-image pre-warm, scripts/prewarm-images.sh (AUD-747).

Same shape as tests/unit/test_ci_phase_ledger.py: the real shipped script runs,
with a stub `docker` ahead of it on PATH, so what is asserted is the script and
not a reimplementation of it. The stub is what makes the interesting cases
reachable at all — a 429 from Docker Hub cannot be summoned on demand, and a
test that really pulled images would spend the very allowance this script exists
to protect.

WHAT IS WORTH TESTING. Not that `docker pull` pulls. The script earns its place
by three properties, and each of them silently reverting is a regression nobody
would notice until the next rate-limited afternoon:

  * it does not pull what is already local — the whole claim that this costs
    nothing in the common case, and the thing that would quietly turn the pre-warm
    into a second source of pull traffic;
  * it retries before giving up — the Hub's window is rolling, so the allowance
    that refused one attempt is partly back for the next;
  * when it does give up it says *why*, naming the refusal rather than letting a
    later phase take the blame. That text is the deliverable: the phase ledger is
    the only CI channel readable here without credentials (AUD-714).

And one collection property: the ref list is derived from compose and the
Dockerfiles rather than hardcoded, so a digest bump needs no edit here. A
regression there reintroduces the lazy pull inside a suite phase.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
PREWARM_SCRIPT = REPO_ROOT / "scripts" / "prewarm-images.sh"

# What the stub's `docker compose config --images` answers with: two external
# refs plus one image Compose would build itself, named after the project the
# script passes. The built one must never be pulled — it exists in no registry.
COMPOSE_IMAGES = """audr-prewarm-backend-tests
postgres:16-alpine
node:22-alpine@sha256:aaaa
"""

DOCKERFILE = """# syntax=docker/dockerfile:1
FROM node:22-alpine AS frontend-builder
RUN echo build
FROM python:3.14-slim AS runtime
COPY --from=frontend-builder /app /app
"""

DOCKERFILE_TEST = "FROM python:3.14-slim@sha256:bbbb\nRUN echo test\n"


def make_repo(tmp_path: Path, *, local: tuple[str, ...] = (), pull_fails: int = 0) -> Path:
    """A throwaway repo root holding the real script and a stub `docker`.

    `local` is the set of refs the stub reports as already in the image store;
    `pull_fails` is how many times each `docker pull` fails before succeeding
    (a number larger than the attempt budget means it never succeeds).
    """
    (tmp_path / "scripts").mkdir()
    shutil.copy(PREWARM_SCRIPT, tmp_path / "scripts" / "prewarm-images.sh")
    (tmp_path / "compose.test.yaml").write_text("# content is irrelevant: the stub answers\n")
    (tmp_path / "Dockerfile").write_text(DOCKERFILE)
    (tmp_path / "Dockerfile.test").write_text(DOCKERFILE_TEST)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "compose-images").write_text(COMPOSE_IMAGES)
    (bin_dir / "local-images").write_text("\n".join(local) + "\n")
    docker = bin_dir / "docker"
    docker.write_text(
        f"""#!/usr/bin/env bash
# Stub docker. Records every pull, answers `inspect` from local-images, and
# answers `compose config --images` from compose-images.
BIN="{bin_dir}"
case "$1" in
  compose)
    for arg in "$@"; do [[ "$arg" == "--images" ]] && cat "$BIN/compose-images" && exit 0; done
    exit 0
    ;;
  image)
    # docker image inspect <ref>
    grep -Fxq "$3" "$BIN/local-images" && exit 0
    exit 1
    ;;
  pull)
    ref="$2"
    echo "$ref" >>"$BIN/pulls.log"
    seen=$(grep -Fxc "$ref" "$BIN/pulls.log")
    if (( seen <= {pull_fails} )); then
      echo "Error response from daemon: toomanyrequests: 429" >&2
      exit 1
    fi
    echo "Status: Downloaded newer image for $ref"
    exit 0
    ;;
esac
exit 0
"""
    )
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return tmp_path


def run_prewarm(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> tuple[int, str, list[str]]:
    """Run the script; return (rc, combined output, refs the stub was asked to pull)."""
    full_env = {
        **os.environ,
        "PATH": f"{repo / 'bin'}:{os.environ['PATH']}",
        # No sleeping in tests: the backoff is the one thing here that would
        # otherwise make the suite a minute slower for nothing.
        "AUDR_PULL_BACKOFF": "0",
        **(env or {}),
    }
    proc = subprocess.run(  # noqa: S603 — argv is the tracked script under tmp_path
        ["bash", str(repo / "scripts" / "prewarm-images.sh"), *args],  # noqa: S607 — `bash` from PATH, as CI runs it
        capture_output=True,
        text=True,
        env=full_env,
        cwd=repo,
        timeout=120,
    )
    pulls_log = repo / "bin" / "pulls.log"
    pulls = pulls_log.read_text().split() if pulls_log.exists() else []
    return proc.returncode, proc.stdout + proc.stderr, pulls


def test_list_collects_compose_and_dockerfile_refs(tmp_path: Path) -> None:
    """Derived, not hardcoded — a digest bump must need no edit to this script."""
    rc, out, _ = run_prewarm(make_repo(tmp_path), "--list")

    assert rc == 0
    assert set(out.split()) == {
        "postgres:16-alpine",
        "node:22-alpine@sha256:aaaa",
        "node:22-alpine",
        "python:3.14-slim",
        "python:3.14-slim@sha256:bbbb",
    }


def test_list_excludes_the_images_compose_builds_itself(tmp_path: Path) -> None:
    """`<project>-<service>` exists in no registry; pulling it is a guaranteed failure."""
    _, out, _ = run_prewarm(make_repo(tmp_path), "--list")

    assert "audr-prewarm-backend-tests" not in out


def test_list_excludes_from_stage_aliases(tmp_path: Path) -> None:
    """`COPY --from=frontend-builder` names a stage, not something to pull."""
    _, out, _ = run_prewarm(make_repo(tmp_path), "--list")

    assert "frontend-builder" not in out
    assert "runtime" not in out.split()


def test_frontend_only_does_not_pull_the_backend_build_bases(tmp_path: Path) -> None:
    """`--frontend-only` builds nothing, so the Dockerfile bases are not its cost."""
    _, out, _ = run_prewarm(make_repo(tmp_path), "--frontend", "--list")

    refs = set(out.split())
    assert "python:3.14-slim@sha256:bbbb" not in refs
    assert "node:22-alpine@sha256:aaaa" in refs


def test_already_local_images_are_not_pulled(tmp_path: Path) -> None:
    """The central claim: no pull traffic, and no allowance spent, in the common case."""
    local = (
        "postgres:16-alpine",
        "node:22-alpine@sha256:aaaa",
        "node:22-alpine",
        "python:3.14-slim",
        "python:3.14-slim@sha256:bbbb",
    )
    rc, out, pulls = run_prewarm(make_repo(tmp_path, local=local))

    assert rc == 0
    assert pulls == [], "a pre-warm that re-pulls local images is a second source of 429s"
    assert "All base images present locally" in out


def test_a_rate_limited_pull_is_retried_and_can_succeed(tmp_path: Path) -> None:
    """The Hub's window is rolling: the attempt that failed is worth making again."""
    repo = make_repo(tmp_path, local=("postgres:16-alpine",), pull_fails=1)
    rc, _, pulls = run_prewarm(repo, "--frontend")

    assert rc == 0
    assert pulls.count("node:22-alpine@sha256:aaaa") == 2, "failed once, so exactly one retry"


def test_exhausted_retries_fail_loudly_and_name_the_cause(tmp_path: Path) -> None:
    """The failure text is the deliverable — it is what replaces a wrong diagnosis."""
    repo = make_repo(tmp_path, pull_fails=99)
    rc, out, pulls = run_prewarm(repo, "--frontend", env={"AUDR_PULL_ATTEMPTS": "2"})

    assert rc == 1
    assert pulls.count("node:22-alpine@sha256:aaaa") == 2, "the attempt budget is honoured"
    assert "IMAGE PRE-WARM FAILED" in out
    assert "node:22-alpine@sha256:aaaa" in out
    assert "429" in out, "the reader must be told it is the registry, not the tree"
    assert "AUD-747" in out


def test_skip_env_restores_lazy_pulling_without_failing(tmp_path: Path) -> None:
    rc, out, pulls = run_prewarm(make_repo(tmp_path), env={"AUDR_SKIP_PREWARM": "1"})

    assert rc == 0
    assert pulls == []
    assert "skipped" in out


def test_resolving_no_images_warns_instead_of_blocking_the_suite(tmp_path: Path) -> None:
    """A silent empty list would restore lazy pulls; a red here would block a good tree."""
    repo = make_repo(tmp_path)
    (repo / "bin" / "compose-images").write_text("")
    (repo / "Dockerfile").unlink()
    (repo / "Dockerfile.test").unlink()

    rc, out, pulls = run_prewarm(repo)

    assert rc == 0
    assert pulls == []
    assert "WARNING" in out


def test_unknown_option_is_refused(tmp_path: Path) -> None:
    """test.sh builds the argv, so a typo there must not read as 'pull everything'."""
    rc, out, _ = run_prewarm(make_repo(tmp_path), "--backend-only")

    assert rc == 2
    assert "Unknown option" in out
