"""Base-image digest pins stay pinned, stay paired, and stay documented (AUD-697).

Same shape as tests/unit/test_version.py: the real shipped files are read from
the repo, so what is asserted is the tree rather than a copy of it. The files
outside ./backend are bind-mounted into `backend-tests` by compose.test.yaml for
exactly that reason — without the mounts these guards could only skip, and a
skipped guard is the failure mode they exist to prevent.

WHAT IS WORTH TESTING HERE, and why a comment was not enough.

AUD-697 pinned every base image by `@sha256:` digest and pinned several of them
to the *same* digest on purpose, because the pairs are not independent:

    Dockerfile `backend-builder` == `runtime`   the venv `runtime` copies out is
                                               built against that interpreter
    `runtime` == Dockerfile.test               the suite gates the image it
                                               tests, so a green suite against
                                               a different CPython proves less
    Dockerfile `frontend-builder`
        == compose.test.yaml `frontend-tests`  Vitest and the shipped bundle
    compose.yaml `db`
        == compose.test.yaml `db-test`         the suite runs against the engine
                                               production runs

That invariant was written into four separate comments ("a bump here is not
finished until its partner moves too") and nothing enforced it. It is also the
invariant most likely to be broken, because AUD-697 added a `docker` Dependabot
ecosystem in the same change and Dependabot reads `FROM` lines only: the very
first pull request it opens for `node:22-alpine` will move Dockerfile and leave
compose.test.yaml behind, and the suite would go on passing against a different
Node than the one that built the bundle. A comment cannot catch that. This can.

The third guard covers how the drift was *discovered*. docs/third-party.md 1.1
is a hand-maintained digest table, and when AUD-697 went to use it, it was stale
in three rows at once — the recorded `python:3.14-slim` digest was four weeks and
two revisions behind what the tree pinned. Nothing failed when it drifted, so it
drifted, and then it got cited. A digest table that is wrong is worse than no
table. So the table is checked against the tree, which is the source of truth.

Deliberately NOT asserted: *which* digest any image is pinned to. That is
Dependabot's job to move and a reviewer's to approve; pinning the value here
would mean every legitimate bump needs a test edit in the same commit, which is
how a guard trains people to update it without reading it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

DOCKERFILE = REPO_ROOT / "Dockerfile"
DOCKERFILE_TEST = REPO_ROOT / "Dockerfile.test"
COMPOSE = REPO_ROOT / "compose.yaml"
COMPOSE_TEST = REPO_ROOT / "compose.test.yaml"
THIRD_PARTY = REPO_ROOT / "docs" / "third-party.md"

# `FROM <image>[@digest] AS <stage>` — the stage name is optional in general but
# every stage in this Dockerfile has one.
FROM_RE = re.compile(
    r"^FROM\s+(?P<ref>\S+?)(?:@(?P<digest>sha256:[0-9a-f]{64}))?(?:\s+AS\s+(?P<stage>\S+))?\s*$"
)
# `  image: <image>[@digest]` under a compose service.
IMAGE_RE = re.compile(r"^\s+image:\s+(?P<ref>\S+?)(?:@(?P<digest>sha256:[0-9a-f]{64}))?\s*$")
SERVICE_RE = re.compile(r"^  (?P<name>[A-Za-z0-9_-]+):\s*$")

# Compose services whose `image:` is built by us rather than pulled from a
# public registry, so a public digest is not a thing they could carry. `api` and
# `worker` in compose.yaml share an anchor pointing at our own GHCR tag, which
# scripts/promote-image.py rewrites per release and test_version.py already
# guards.
LOCALLY_BUILT_PREFIXES = ("ghcr.io/br3d/", "audr-", "${")


def _skip_if_unmounted(path: Path) -> None:
    """A missing file means the bind mount is gone, which is itself the finding.

    Phrased as a failure rather than a skip on purpose: compose.test.yaml mounts
    every file this module reads, so absence means somebody removed a mount and
    silently disarmed a guard. Skipping would hide that, which is the whole
    complaint these tests were written about.
    """
    assert path.exists(), (
        f"{path} is not readable from the test container. compose.test.yaml must "
        f"bind-mount it into backend-tests — without the mount this guard can only "
        f"skip, and a skipped pin guard is indistinguishable from a passing one."
    )


def dockerfile_pins(path: Path) -> dict[str, tuple[str, str | None]]:
    """Map stage name -> (image ref without digest, digest or None)."""
    _skip_if_unmounted(path)
    pins: dict[str, tuple[str, str | None]] = {}
    for i, line in enumerate(path.read_text().splitlines()):
        m = FROM_RE.match(line)
        if not m:
            continue
        # An unnamed stage is keyed by its line number so two of them cannot
        # collide and silently drop one from the checks below.
        stage = m["stage"] or f"<unnamed:{i + 1}>"
        pins[stage] = (m["ref"], m["digest"])
    return pins


def compose_pins(path: Path) -> dict[str, tuple[str, str | None]]:
    """Map compose service name -> (image ref without digest, digest or None)."""
    _skip_if_unmounted(path)
    pins: dict[str, tuple[str, str | None]] = {}
    service: str | None = None
    for line in path.read_text().splitlines():
        sm = SERVICE_RE.match(line)
        if sm:
            service = sm["name"]
            continue
        im = IMAGE_RE.match(line)
        if im and service is not None:
            pins[service] = (im["ref"], im["digest"])
    return pins


def test_every_external_base_image_is_digest_pinned() -> None:
    """A tag-only reference means two builds of one commit can differ (AUD-697)."""
    unpinned: list[str] = []

    for path in (DOCKERFILE, DOCKERFILE_TEST):
        for stage, (ref, digest) in dockerfile_pins(path).items():
            if digest is None:
                unpinned.append(f"{path.name} stage {stage}: {ref}")

    for path in (COMPOSE, COMPOSE_TEST):
        for service, (ref, digest) in compose_pins(path).items():
            if ref.startswith(LOCALLY_BUILT_PREFIXES):
                continue
            if digest is None:
                unpinned.append(f"{path.name} service {service}: {ref}")

    assert not unpinned, (
        "these base images are referenced by tag with no `@sha256:` digest, so two "
        "builds of the same commit can land on different base revisions:\n  "
        + "\n  ".join(unpinned)
    )


def test_digests_that_must_not_drift_apart_are_equal() -> None:
    """The pairs pinned to one digest on purpose. See this module's docstring."""
    df = dockerfile_pins(DOCKERFILE)
    dft = dockerfile_pins(DOCKERFILE_TEST)
    cmp_ = compose_pins(COMPOSE)
    cmpt = compose_pins(COMPOSE_TEST)

    # Dockerfile.test has a single unnamed stage.
    (test_image_digest,) = [digest for _, digest in dft.values()]

    groups: list[tuple[str, dict[str, str | None]]] = [
        (
            "python: the venv is built against backend-builder's interpreter, the "
            "runtime image copies it out, and the suite gates that image — so all "
            "three must be one CPython build",
            {
                "Dockerfile backend-builder": df["backend-builder"][1],
                "Dockerfile runtime": df["runtime"][1],
                "Dockerfile.test": test_image_digest,
            },
        ),
        (
            "node: Vitest must run on the same Node that builds the shipped bundle",
            {
                "Dockerfile frontend-builder": df["frontend-builder"][1],
                "compose.test.yaml frontend-tests": cmpt["frontend-tests"][1],
            },
        ),
        (
            "postgres: the suite must run against the engine production runs",
            {
                "compose.yaml db": cmp_["db"][1],
                "compose.test.yaml db-test": cmpt["db-test"][1],
            },
        ),
    ]

    for why, members in groups:
        distinct = set(members.values())
        assert len(distinct) == 1, (
            f"these pins must share one digest and do not — {why}.\n  "
            + "\n  ".join(f"{k}: {v}" for k, v in members.items())
            + "\nDependabot reads Dockerfile `FROM` lines only, so a bump it opens "
            "leaves the compose-side pin behind; move both in one commit and "
            "update docs/third-party.md 1.1."
        )


def test_third_party_doc_records_the_digests_the_tree_actually_pins() -> None:
    """The 1.1 table was stale in three rows when AUD-697 came to use it.

    Nothing failed when it drifted, so it drifted — and then it was cited. The
    table is a transcription of the tree, so the tree is what it is checked
    against.
    """
    _skip_if_unmounted(THIRD_PARTY)
    doc = THIRD_PARTY.read_text()

    df = dockerfile_pins(DOCKERFILE)
    cmp_ = compose_pins(COMPOSE)
    pinned = {
        "python:3.14-slim": df["runtime"][1],
        "node:22-alpine": df["frontend-builder"][1],
        "postgres:16-alpine": cmp_["db"][1],
    }

    missing = {image: digest for image, digest in pinned.items() if digest and digest not in doc}
    assert not missing, (
        "docs/third-party.md section 1.1 does not record the digest the tree pins "
        "for:\n  "
        + "\n  ".join(f"{image} -> {digest}" for image, digest in missing.items())
        + "\nUpdate that table in the same commit as the pin. A digest table that is "
        "wrong is worse than no table, because it gets cited."
    )
