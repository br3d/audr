#!/usr/bin/env python3
"""Unit tests for scripts/promote-image.py (AUD-546).

What is covered is the half that can be got wrong silently: the `compose.yaml`
rewrite. Every failure here lands a broken anchor or a healthcheck calling a
module the pinned image does not have, which is the exact class of bug this
script exists to end. The registry half is left to the real registry; it fails
loudly and in one place.

Moved here from `scripts/` by AUD-698, where it ran in NO job — see
test_registry_prune.py for the history. Sibling file:
test_promote_image_drift.py, which was written in backend/tests/unit from the
start because this one was unreachable.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

# Required: backend-tests runs `-m "unit or integration or contract"`, so an
# unmarked file is collected and then deselected — moved and still not run.
pytestmark = pytest.mark.unit

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_SCRIPT = REPO_ROOT / "scripts" / "promote-image.py"

# Two separate mounts, so they are reported separately: the script is the
# subject, and compose.yaml is the fixture every rewrite case reads.
if not _SCRIPT.exists():  # pragma: no cover - mount missing
    pytest.skip(
        "scripts/promote-image.py is not mounted into the test container; add it "
        "to compose.test.yaml's backend-tests volumes",
        allow_module_level=True,
    )
if not (REPO_ROOT / "compose.yaml").exists():  # pragma: no cover - mount missing
    pytest.skip(
        "compose.yaml is not mounted into the test container; add it to "
        "compose.test.yaml's backend-tests volumes",
        allow_module_level=True,
    )

_spec = importlib.util.spec_from_file_location("promote_image", _SCRIPT)
pi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pi)

COMPOSE = (REPO_ROOT / "compose.yaml").read_text()

# The healthcheck-revert cases below only mean anything while compose.yaml
# actually carries the inline probes, and today it does not: AUD-442 inlined
# them temporarily, AUD-546 built this revert path, and the revert has since
# happened — compose.yaml calls `python -m audr.operations.healthcheck`
# directly. With nothing to restore, `rewrite_compose(restore_api=True)` is a
# no-op and four cases here assert changes that cannot occur.
#
# They had been failing for exactly as long, unnoticed, because this file ran in
# no job (AUD-698) — which is a tidy demonstration of why it was worth wiring in
# rather than deleting. Skipped rather than dropped, on the same reasoning as
# test_healthcheck.py, which guards the other half of this mechanism and skips
# the mirrored cases with "compose.yaml calls the module directly": the inline
# form comes back the next time a publish has to be pinned before the tag
# carrying the module exists, and the revert path has to still be tested then.
INLINE_PRESENT = pi.API_INLINE_ANCHOR in COMPOSE
needs_inline = pytest.mark.skipif(
    not INLINE_PRESENT,
    reason="compose.yaml calls the module directly; there is no inline probe to revert",
)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# --- the pin ----------------------------------------------------------------


def test_bumps_the_anchor() -> None:
    out, changes = pi.rewrite_compose(COMPOSE, "9.9.9", restore_api=False, restore_worker=False)
    check(f"{pi.IMAGE}:9.9.9" in out, "anchor was not rewritten")
    check(any("x-backend-image" in c for c in changes), f"change not reported: {changes}")
    # One anchor, three services — the services must keep reading it, not a copy.
    check(out.count("ghcr.io/br3d/audr-backend:") == 1, "more than one literal image reference")


def test_is_idempotent() -> None:
    """Running it twice is a no-op, which is what makes it safe to re-run."""
    once, _ = pi.rewrite_compose(COMPOSE, "9.9.9", restore_api=True, restore_worker=True)
    twice, changes = pi.rewrite_compose(once, "9.9.9", restore_api=True, restore_worker=True)
    check(twice == once, "second run changed the file again")
    check(changes == [], f"second run reported changes: {changes}")


def test_rejects_a_file_with_no_anchor() -> None:
    try:
        pi.rewrite_compose(
            "services:\n  api:\n    image: nginx\n",
            "1.0.0",
            restore_api=False,
            restore_worker=False,
        )
    except pi.Refused as exc:
        check("x-backend-image" in str(exc), f"unhelpful message: {exc}")
        return
    raise AssertionError("a compose.yaml with no anchor was accepted")


# --- the healthcheck reverts ------------------------------------------------


@needs_inline
def test_restores_both_healthchecks() -> None:
    out, changes = pi.rewrite_compose(COMPOSE, "9.9.9", restore_api=True, restore_worker=True)
    live = pi.uncommented(out)
    check(pi.API_MODULE_FORM in live, "api healthcheck not restored")
    check(pi.WORKER_MODULE_FORM in live, "worker healthcheck not restored")
    check(len(changes) == 3, f"expected pin + two reverts, got {changes}")
    # The inline probes are gone, not merely shadowed.
    check("urllib.request.urlopen(" not in out, "inline api probe survived the revert")
    check('open("/app/worker-heartbeat")' not in out, "inline worker probe survived the revert")


@needs_inline
def test_leaves_healthchecks_alone_when_the_tag_lacks_the_module() -> None:
    """The AUD-544 guard: an unverified tag must not get the module form.

    Promoting to a tag that predates `--worker` while swapping the probe in is
    how `worker` ends up permanently `unhealthy` against a perfectly live
    container, so the two flags are independent and default to not touching it.
    """
    out, _ = pi.rewrite_compose(COMPOSE, "9.9.9", restore_api=False, restore_worker=False)
    live = pi.uncommented(out)
    check(pi.API_MODULE_FORM not in live, "api healthcheck restored against an unverified tag")
    check(pi.WORKER_MODULE_FORM not in live, "worker healthcheck restored, unverified tag")
    check(pi.API_INLINE_ANCHOR in out, "api inline marker lost")
    check(pi.WORKER_INLINE_ANCHOR in out, "worker inline marker lost")


@needs_inline
def test_restores_api_without_worker() -> None:
    """The real intermediate case: a tag with the module but not the beacon writer."""
    out, changes = pi.rewrite_compose(COMPOSE, "9.9.9", restore_api=True, restore_worker=False)
    live = pi.uncommented(out)
    check(pi.API_MODULE_FORM in live, "api healthcheck not restored")
    check(pi.WORKER_MODULE_FORM not in live, "worker healthcheck restored when it should not be")
    check(len(changes) == 2, f"expected pin + api revert, got {changes}")


@needs_inline
def test_rejects_a_third_healthcheck_shape() -> None:
    """Neither module nor a matchable inline block: stop, do not guess."""
    mangled = COMPOSE.replace(pi.API_INLINE_ANCHOR, "# someone reworded this")
    try:
        pi.rewrite_compose(mangled, "9.9.9", restore_api=True, restore_worker=False)
    except pi.Refused as exc:
        check("third shape" in str(exc), f"unhelpful message: {exc}")
        return
    raise AssertionError("a compose.yaml with an unrecognisable healthcheck was accepted")


# --- the "does this tag carry it" predicates --------------------------------


def test_worker_gate_needs_both_halves() -> None:
    """`--worker` alone is not enough; something must write the beacon (AUD-544)."""
    seen: dict[str, str | None] = {}

    def fake_blob(_root: object, _commit: str, path: str) -> str | None:
        return seen.get(path)

    original = pi.blob_at
    pi.blob_at = fake_blob  # type: ignore[assignment]
    try:
        seen = {"backend/src/audr/operations/healthcheck.py": 'if "--worker" in argv:'}
        check(not pi.carries_worker_healthcheck(REPO_ROOT, "x"), "accepted a flag with no writer")

        seen["backend/src/audr/jobs/__main__.py"] = "WORKER_HEARTBEAT_PATH.write_text(str(t))"
        check(pi.carries_worker_healthcheck(REPO_ROOT, "x"), "rejected a tag carrying both halves")

        seen["backend/src/audr/operations/healthcheck.py"] = "def main(): ..."
        check(not pi.carries_worker_healthcheck(REPO_ROOT, "x"), "accepted a module with no flag")

        seen = {}
        check(not pi.carries_worker_healthcheck(REPO_ROOT, "x"), "accepted a tag with no module")
        check(not pi.carries_api_healthcheck(REPO_ROOT, "x"), "accepted a tag with no module")
    finally:
        pi.blob_at = original  # type: ignore[assignment]


def test_head_of_main_carries_both() -> None:
    """Sanity: the predicates agree with the tree they are reading right now.

    Alone in this file in needing a git checkout — `carries_*_healthcheck` reads
    its blobs with `git show <commit>:<path>`. The backend test container mounts
    `./backend` and a handful of individual files, not `.git`, so this one case
    is skipped there rather than failing. It still bites where it can: a local
    `./scripts/test.sh` run and anyone running this file by hand.
    """
    try:
        subprocess.run(  # noqa: S603 — argv is a constant plus the resolved repo root
            ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-dir"],  # noqa: S607 — `git` from PATH
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):  # pragma: no cover - container
        pytest.skip(
            "not a git checkout (the test container mounts no .git), and these "
            "two predicates read blobs with `git show`"
        )
    check(pi.carries_api_healthcheck(REPO_ROOT, "HEAD"), "HEAD has no healthcheck module?")
    check(pi.carries_worker_healthcheck(REPO_ROOT, "HEAD"), "HEAD has no worker beacon?")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {test.__name__}: {exc}")
        else:
            print(f"ok   {test.__name__}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
