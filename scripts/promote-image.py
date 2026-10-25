#!/usr/bin/env python3
"""Step 2 of a release: point `main` at an image that actually exists (AUD-546).

    ./scripts/promote-image.py            # check + rewrite, nothing pushed
    ./scripts/promote-image.py --push     # ... and push main
    ./scripts/promote-image.py --check    # verify only, never touch the tree

## Why this is a separate script

`scripts/release.sh` used to rewrite `compose.yaml`'s `x-backend-image` anchor
to the version it was cutting and push that to `main`. Every other file it
touches is a claim about the repository and is true the instant the commit is
made; the anchor is a claim about an *external registry*, and it was false
until somebody dispatched `release.yml` in the GitHub UI. Between the two,
`main` named an image that did not exist and a fresh clone died on
`docker compose up -d` with `manifest unknown`. `v0.1.2` sat that way for nine
days (AUD-442).

The two steps cannot be made atomic from this end: `release.yml` is
`workflow_dispatch`-only by deliberate design (a `tags:` trigger is forbidden —
the GitHub->Gitea mirror would replay it onto our self-hosted runner), so the
publish needs a human in a browser. What *can* be fixed is the ordering, and
that is this script: the pin bump is no longer part of the release commit, it
is a follow-up commit gated on the image being real. `main` is therefore
installable at every commit — the pin may trail `VERSION` while a release is
cut but unpublished, never lead it (CEO ruling on AUD-545,
`backend/tests/unit/test_version.py::test_compose_image_tag_matches`).

## What "the image is real" means here

Not just "the tag resolves". Three things are checked, because the healthcheck
reverts below are only safe if the published image genuinely carries the code
they call:

1. `ghcr.io/br3d/audr-backend:<version>` has a manifest. Note the full `Accept`
   header in `_manifest()` — GHCR answers 404 for tags that exist when asked
   without it, so a bare GET would report a published release as missing.
2. That image's `org.opencontainers.image.revision` label equals the commit
   `v<version>` points at locally. This is what rules out "someone dispatched
   the workflow against `main`": that build publishes `<version>-g<sha12>`, not
   the bare tag, but the label check means we never have to trust that rule
   held — a bare tag built from any other commit is refused by name.
3. Given (2), the contents of the image are the contents of that commit, so
   whether it carries `audr.operations.healthcheck`, its `--worker` flag, and
   the beacon writer in `audr.jobs.__main__` is a question `git cat-file` can
   answer offline. Each healthcheck revert is gated on its own answer.

A fourth check asks a different question — not "is this image the release?" but
"is this release still the one to pin?". `v<version>` must be an ancestor of
`main` and no more than `MAX_DRIFT` commits behind it, because promotion is what
decides which backend every future `git clone && docker compose up -d` installs.
See the comment above `MAX_DRIFT` for the near-miss that put it here.

(3) is what closes AUD-544: the worker's inline probe is only swapped for
`python -m audr.operations.healthcheck --worker` against a *published* tag
proven to contain both the flag and the writer that feeds it, so the false
`unhealthy` that swap would have caused against 0.1.1 or v0.1.2 cannot recur.

Python rather than the `.sh` the issue sketched: the registry dance and the
`compose.yaml` rewrite are both easier to get right — and, more to the point,
testable without a network — in Python. `scripts/test_promote_image.py` covers
the rewrite; the convention is the same as `registry-prune.py`.

Idempotent: run it twice and the second run finds nothing to change and exits
0. Run it before the dispatch and it refuses, naming the dispatch.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys
import urllib.error
import urllib.request

REGISTRY = "https://ghcr.io"
REPO = "br3d/audr-backend"
IMAGE = f"ghcr.io/{REPO}"
DISPATCH_URL = "https://github.com/br3d/audr/actions/workflows/release.yml"

# Every media type GHCR may answer with. Omitting any of these is the trap
# described in the module docstring: the registry 404s rather than negotiating
# down, so a tag that exists reads as absent.
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
REVISION_LABEL = "org.opencontainers.image.revision"

TIMEOUT = 30


class Refused(Exception):
    """A release-blocking condition, already phrased for the operator."""


# --- registry ---------------------------------------------------------------


def _get(url: str, *, accept: str, token: str | None = None) -> bytes:
    headers = {"Accept": accept}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)  # noqa: S310 - https literal above
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return response.read()


def anonymous_token() -> str:
    """A pull token for the public repo. GHCR issues these without credentials."""
    url = f"{REGISTRY}/token?service=ghcr.io&scope=repository:{REPO}:pull"
    return str(json.loads(_get(url, accept="application/json"))["token"])


def _manifest(reference: str, token: str) -> dict:
    url = f"{REGISTRY}/v2/{REPO}/manifests/{reference}"
    return json.loads(_get(url, accept=ACCEPT, token=token))


def image_revision(version: str, token: str) -> str:
    """The `org.opencontainers.image.revision` label on `:<version>`.

    Raises `Refused` with the dispatch instructions when the tag is not there.
    """
    try:
        manifest = _manifest(version, token)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403, 404):
            raise Refused(
                f"{IMAGE}:{version} is not in the registry (HTTP {exc.code}).\n"
                "\n"
                "The release was cut but never published. Publishing is a manual\n"
                "dispatch — this script cannot do it, and will not move the pin until\n"
                "someone has:\n"
                "\n"
                f"    {DISPATCH_URL}\n"
                f"    Run workflow, ref v{version}\n"
                "\n"
                "Re-run this script once that run is green. Until then `main` keeps\n"
                "pointing at the last published image, which is the correct state: a\n"
                "clone that installs one release behind beats a clone that cannot\n"
                "install at all (AUD-442, AUD-545)."
            ) from exc
        raise

    # A multi-arch index points at per-platform manifests; the labels live on
    # the platform image's config, so descend. buildx also attaches attestation
    # manifests with platform unknown/unknown — skip those or we read a config
    # that has no labels at all.
    if "manifests" in manifest:
        children = [
            child
            for child in manifest["manifests"]
            if (child.get("platform") or {}).get("os") not in (None, "unknown")
        ]
        if not children:
            raise Refused(
                f"{IMAGE}:{version} is an index with no platform manifest — only "
                "attestations. The publish did not produce a usable image."
            )
        manifest = _manifest(children[0]["digest"], token)

    digest = (manifest.get("config") or {}).get("digest")
    if not digest:
        raise Refused(f"{IMAGE}:{version} has a manifest with no config blob.")
    config = json.loads(
        _get(f"{REGISTRY}/v2/{REPO}/blobs/{digest}", accept="application/json", token=token)
    )
    labels = (config.get("config") or {}).get("Labels") or {}
    revision = labels.get(REVISION_LABEL, "")
    if not revision:
        raise Refused(
            f"{IMAGE}:{version} carries no {REVISION_LABEL} label, so there is no way "
            "to tell which commit it was built from. Refusing to point `main` at an "
            "image of unknown provenance — rebuild it with the current Dockerfile, "
            "which sets the label from AUDR_GIT_SHA."
        )
    return str(revision)


# --- git --------------------------------------------------------------------


def git(root: pathlib.Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def blob_at(root: pathlib.Path, commit: str, path: str) -> str | None:
    """File contents at a commit, or None when the file is not there."""
    try:
        return git(root, "show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return None


def release_commit(root: pathlib.Path, version: str) -> str:
    try:
        return git(root, "rev-parse", f"refs/tags/v{version}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise Refused(
            f"No local tag v{version}. Promotion verifies the published image against "
            "the commit its release tag names, so the tag has to be here: "
            "`git fetch --tags`, or cut the release first with "
            "`./scripts/release.sh patch --push`."
        ) from exc


# --- how far behind `main` the release being promoted is ---------------------
#
# Everything above proves the published image *is* the release tag. None of it
# asks whether that tag is still worth pinning, and promotion is exactly where
# that question gets answered for every future clone: `x-backend-image` is what
# `docker compose up -d` pulls, so a stale promotion is a stale install.
#
# This is not hypothetical. On 2026-10-18 v0.1.2 was a cut-but-unpublished tag
# sitting 106 commits behind `main`, and the last step anyone had written down
# was "dispatch the publish, then promote". The image would have been genuine,
# built from the v0.1.2 commit, carrying the healthcheck module — so all three
# checks above pass — and promoting it would have pinned fresh clones to a
# seven-day-old backend missing the AUD-597 migration-rollback fix, the AUD-595
# spool fixes and the AUD-562 backup work. That trade was caught by hand and
# refused; a tag cut once and published days later is the normal shape of the
# `workflow_dispatch` gate, so it will come round again. Hence a guard.

# The healthy flow leaves `main` 0-3 commits ahead of the tag at promotion time
# (release.sh commits the tag itself; anything merged while the dispatch waits
# adds to the count). The point is to fire on "the release has been overtaken"
# and stay quiet on "a couple of things landed during the dispatch".
#
# Raised 20 -> 50 by CEO decision on 2026-10-25 (AUD-745), because 20 was
# calibrated against the zero-lag ideal rather than against how long the click
# actually takes. `main` merges at ~14 commits/day here, so 20 was a **34-hour**
# window on a gate whose observed response time is 3-4 days — and the guard then
# fires on every release as a matter of course. It did: v0.4.0 crossed 20 while
# its publish card sat pending, which is what prompted this. That is a
# miscalibrated threshold, not an overtaken release.
#
# 50 is ~3.5 days at the current merge rate, sized to the real window. It is
# still far below the 106 that prompted the guard, so the case it was built for
# is unchanged. The ancestor check is untouched and does the heavier lifting:
# off-`main` code cannot be promoted at any drift. `--allow-stale` remains an
# emergency override for a deliberately older pin, not the normal path.
#
# If this needs raising a third time, raise the dispatch latency instead — a
# standing publish authorisation removes the race rather than widening it.
MAX_DRIFT = 50

# `origin/main` first: it is what a fresh clone gets, which is whose install
# this pin decides. Local `main` is the fallback for a clone with no remote ref
# fetched (CI checkouts, the stand).
MAIN_REFS = ("refs/remotes/origin/main", "refs/heads/main")


def main_ref(root: pathlib.Path) -> str | None:
    """The most authoritative `main` this clone has, or None if it has neither."""
    for ref in MAIN_REFS:
        try:
            git(root, "rev-parse", "--verify", "--quiet", ref)
        except subprocess.CalledProcessError:
            continue
        return ref
    return None


def measure_drift(root: pathlib.Path, commit: str, ref: str) -> tuple[bool, int]:
    """`(commit is an ancestor of ref, commits on ref since commit)`."""
    ancestor = (
        subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", commit, ref],
            capture_output=True,
        ).returncode
        == 0
    )
    return ancestor, int(git(root, "rev-list", "--count", f"{commit}..{ref}"))


def assess_drift(version: str, ref: str, *, ancestor: bool, behind: int) -> None:
    """Raise `Refused` when `v<version>` is no longer fit to pin. Pure; testable."""
    if not ancestor:
        raise Refused(
            f"tag v{version} is not an ancestor of {ref}.\n"
            "\n"
            "Whatever that image contains, it is not code that is on `main` — so "
            "promoting it would pin every clone to a backend this repository does "
            "not describe. Either the tag was cut on a branch that never merged, or "
            "`main` was rewritten under it.\n"
            "\n"
            "Reconcile the two before promoting. Re-run with --allow-stale only if "
            "you have established that pinning off-main code is what you want."
        )
    if behind > MAX_DRIFT:
        raise Refused(
            f"tag v{version} is {behind} commits behind {ref} (limit {MAX_DRIFT}).\n"
            "\n"
            "The image is genuine, but it is no longer the backend this repository "
            "ships: promoting it writes that stale tag into compose.yaml, and from "
            "then on `git clone && docker compose up -d` installs it.\n"
            "\n"
            "Cut a release from current `main` instead and publish that — "
            "`./scripts/release.sh patch --push`, then dispatch\n"
            "\n"
            f"    {DISPATCH_URL}\n"
            "\n"
            "against the new tag. A release that was overtaken while waiting for its "
            "publish click is better replaced than promoted.\n"
            "\n"
            "--allow-stale overrides this if pinning the older release is deliberate."
        )


# --- compose.yaml rewrite ---------------------------------------------------
#
# Both healthcheck blocks are anchored on the first line of the comment that
# marks them as temporary, and `backend/tests/unit/test_healthcheck.py` fails
# the build if this file and compose.yaml stop agreeing on that line — without
# that guard, rewording the comment would turn the revert into a silent no-op
# and the inline copies would outlive the tag that forced them.

ANCHOR_RE = re.compile(r"^x-backend-image: &backend_image ghcr\.io/br3d/audr-backend:(\S+)$", re.M)

API_INLINE_ANCHOR = "# Inlined, and only until the next publish"
API_MODULE_FORM = 'test: ["CMD", "python", "-m", "audr.operations.healthcheck"]'
WORKER_INLINE_ANCHOR = "# Inlined (worker), and only until the next publish"
WORKER_MODULE_FORM = 'test: ["CMD", "python", "-m", "audr.operations.healthcheck", "--worker"]'


def _inline_block_re(anchor: str) -> re.Pattern[str]:
    """The comment block + inline `test:` list, up to the `interval:` that ends it."""
    return re.compile(
        r"(?:^    " + re.escape(anchor) + r"[^\n]*\n)"
        r"(?:^    #[^\n]*\n)*"
        r"^    healthcheck:\n"
        r"^      test:\n"
        r"(?:^(?: {8}[^\n]*)?\n)+"
        r"(?=^      interval:)",
        re.M,
    )


def uncommented(text: str) -> str:
    """`text` with whole-line YAML comments dropped.

    Load-bearing, not tidiness: the comment block above each inline probe
    quotes the very module invocation it is explaining why it cannot use yet,
    so `module_form in compose_text` is true while the inline copy is still
    there. Asking that question of the comments is how the revert silently
    becomes a no-op. `backend/tests/unit/test_healthcheck.py` strips comments
    for the same reason.
    """
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def restore_healthcheck(text: str, anchor: str, module_form: str) -> tuple[str, bool]:
    """Swap one inlined probe for its module call. Returns (text, changed)."""
    if module_form in uncommented(text):
        return text, False
    new, n = _inline_block_re(anchor).subn(
        f"    healthcheck:\n      {module_form}\n", text, count=1
    )
    if n != 1:
        raise Refused(
            f"compose.yaml's healthcheck marked {anchor!r} is in neither accepted "
            f"shape: not the module form ({module_form}) and not an inline block this "
            "script can match. Someone edited it into a third shape — reconcile it "
            "with backend/src/audr/operations/healthcheck.py by hand."
        )
    return new, True


def rewrite_compose(
    text: str, version: str, *, restore_api: bool, restore_worker: bool
) -> tuple[str, list[str]]:
    """Apply the promotion to compose.yaml's contents. Returns (text, what changed)."""
    changes: list[str] = []

    pins = ANCHOR_RE.findall(text)
    if not pins:
        raise Refused(
            "compose.yaml has no `x-backend-image: &backend_image "
            f"{IMAGE}:<version>` line — that exact spelling is what this script "
            "rewrites and what test_version.py asserts on."
        )
    if len(set(pins)) != 1:
        raise Refused(f"compose.yaml names more than one backend tag: {sorted(set(pins))}.")
    if pins[0] != version:
        text = ANCHOR_RE.sub(f"x-backend-image: &backend_image {IMAGE}:{version}", text, count=1)
        changes.append(f"x-backend-image: {pins[0]} -> {version}")

    if restore_api:
        text, changed = restore_healthcheck(text, API_INLINE_ANCHOR, API_MODULE_FORM)
        if changed:
            changes.append("api healthcheck -> python -m audr.operations.healthcheck")
    if restore_worker:
        text, changed = restore_healthcheck(text, WORKER_INLINE_ANCHOR, WORKER_MODULE_FORM)
        if changed:
            changes.append("worker healthcheck -> python -m audr.operations.healthcheck --worker")

    return text, changes


# --- what the published image is known to contain ---------------------------


def carries_api_healthcheck(root: pathlib.Path, commit: str) -> bool:
    return blob_at(root, commit, "backend/src/audr/operations/healthcheck.py") is not None


def carries_worker_healthcheck(root: pathlib.Path, commit: str) -> bool:
    """Both halves, or the probe is worse than the inline copy it replaces.

    `--worker` without the beacon writer is the AUD-544 failure: the flag parses,
    the file it reads is never written, and `worker` reports `unhealthy` forever
    while being perfectly alive.
    """
    module = blob_at(root, commit, "backend/src/audr/operations/healthcheck.py")
    if not module or "--worker" not in module:
        return False
    writer = blob_at(root, commit, "backend/src/audr/jobs/__main__.py")
    return bool(writer and "WORKER_HEARTBEAT_PATH.write_text" in writer)


# --- main -------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Point compose.yaml at a published image, after proving it exists.",
    )
    parser.add_argument("--version", help="Version to promote to. Default: the VERSION file.")
    parser.add_argument("--push", action="store_true", help="Push main after committing.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the image and report what would change; never write.",
    )
    parser.add_argument(
        "--allow-stale",
        action="store_true",
        help="Promote even if the release tag has been overtaken by main. See MAX_DRIFT.",
    )
    args = parser.parse_args(argv)

    root = pathlib.Path(__file__).resolve().parent.parent
    version = args.version or (root / "VERSION").read_text().strip()

    try:
        return _promote(
            root, version, push=args.push, check=args.check, allow_stale=args.allow_stale
        )
    except Refused as exc:
        print(f"FATAL: {exc}", file=sys.stderr)
        return 1


def _promote(
    root: pathlib.Path, version: str, *, push: bool, check: bool, allow_stale: bool = False
) -> int:
    print(f"==> promoting {IMAGE}:{version}")

    commit = release_commit(root, version)
    revision = image_revision(version, anonymous_token())
    if revision != commit:
        raise Refused(
            f"{IMAGE}:{version} was built from commit {revision[:12]}, but tag v{version} "
            f"points at {commit[:12]}.\n"
            "\n"
            "A bare `:<version>` tag is only supposed to come from the `v<version>` "
            "commit; this one did not, so its contents are not the release. Re-dispatch "
            f"{DISPATCH_URL} against ref v{version}."
        )
    print(f"    registry: present, built from {commit[:12]} (matches v{version})")

    restore_api = carries_api_healthcheck(root, commit)
    restore_worker = carries_worker_healthcheck(root, commit)
    for what, ok in (("api", restore_api), ("worker", restore_worker)):
        print(
            f"    {what} healthcheck module: "
            + ("present in this tag" if ok else "NOT in this tag — inline probe stays")
        )

    compose = root / "compose.yaml"
    original = compose.read_text()
    updated, changes = rewrite_compose(
        original, version, restore_api=restore_api, restore_worker=restore_worker
    )

    if not changes:
        print(f"==> compose.yaml already names {IMAGE}:{version}; nothing to do.")
        return 0
    for change in changes:
        print(f"    {change}")

    # Deliberately after the nothing-to-do exit above. Drift grows on its own
    # as `main` advances, so a promoted release goes stale in place: asking the
    # question on a run that would change nothing would make a second run of an
    # already-correct promotion start failing weeks later, which breaks the
    # idempotence this script's docstring promises.
    ref = main_ref(root)
    if ref is None:
        print("    drift: no main ref in this clone — staleness unchecked")
    else:
        ancestor, behind = measure_drift(root, commit, ref)
        if allow_stale:
            print(
                f"    drift: {behind} commits behind {ref}"
                f"{'' if ancestor else ', NOT an ancestor'} — --allow-stale given"
            )
        else:
            assess_drift(version, ref, ancestor=ancestor, behind=behind)
            print(f"    drift: {behind} commits behind {ref} (limit {MAX_DRIFT})")

    if check:
        print("==> --check: nothing written.")
        return 0

    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    if branch != "main":
        raise Refused(f"promotion lands on main, not '{branch}'. Merge or switch first.")
    dirty = git(root, "status", "--porcelain")
    if dirty:
        raise Refused(f"working tree is dirty — commit or stash first:\n{dirty}")

    compose.write_text(updated)
    git(root, "add", "compose.yaml")
    git(
        root,
        "commit",
        "-m",
        f"Promote compose.yaml to the published {IMAGE}:{version}",
        "-m",
        f"Verified present in GHCR and built from {commit[:12]} (tag v{version}).",
    )
    print("==> committed")

    if push:
        git(root, "push", "origin", "main")
        print("==> pushed main — a fresh clone now installs this release.")
    else:
        print("==> nothing pushed. `git push origin main` when ready.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
