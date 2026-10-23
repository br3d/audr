"""Version is one number in four files, and the API must report it (AUD-407).

The duplication is forced — see backend/src/audr/version.py for why none of the
four can read from another at runtime — so these tests are what keeps it from
becoming four different numbers. Drift here is silent and expensive: an image
tagged 1.4.2 whose sidebar says 1.4.1 makes every deploy report untrustworthy,
which is the exact problem semantic versioning was adopted to solve.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from audr.api.version import router as version_router
from audr.version import __version__, get_build_info

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]

SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


def test_version_is_semver() -> None:
    assert SEMVER_RE.match(__version__), f"{__version__!r} is not a semantic version"


def test_version_file_matches() -> None:
    on_disk = (REPO_ROOT / "VERSION").read_text().strip()
    assert on_disk == __version__, (
        f"VERSION says {on_disk}, audr.version says {__version__} — "
        "bump with scripts/release.sh, which rewrites both."
    )


def test_committed_openapi_snapshot_carries_this_version() -> None:
    """backend/openapi.json is a committed snapshot of `app.openapi()`
    (AUD-694), and FastAPI fills its `info.version` from `__version__` — so it
    is a version carrier like any other, and scripts/release.sh rewrites it.

    test_export_openapi.py::test_committed_schema_matches_the_live_app already
    fails when the two disagree, but it compares whole documents and reports a
    truncated dict diff over ~3800 lines. That is how v0.4.0 shipped a red
    suite nobody could read at a glance, and because build-deploy `needs:` the
    suite, the stand stayed undeployed (AUD-720). This test names the number.
    """
    snapshot = json.loads((REPO_ROOT / "backend" / "openapi.json").read_text())
    assert snapshot["info"]["version"] == __version__, (
        f"backend/openapi.json says {snapshot['info']['version']}, "
        f"audr.version says {__version__} — regenerate the snapshot with "
        "`python -m audr.operations.export_openapi backend/openapi.json`, or "
        "bump with scripts/release.sh, which rewrites it."
    )


def test_pyproject_matches() -> None:
    data = tomllib.loads((REPO_ROOT / "backend/pyproject.toml").read_text())
    assert data["project"]["version"] == __version__


def test_package_json_matches() -> None:
    data = json.loads((REPO_ROOT / "frontend/package.json").read_text())
    assert data["version"] == __version__, (
        "frontend/package.json drives the version baked into the SPA bundle "
        "(frontend/build-meta.ts), so the sidebar would show the wrong number."
    )


def test_compose_image_tag_matches() -> None:
    """`docker compose up -d` on a checkout must pull an image that exists.

    compose.yaml names the published image literally (AUD-418, AUD-439) so an
    operator can see what will be downloaded, which makes it look like a fifth
    version carrier: the `x-backend-image` anchor the three backend services
    share. It is not one. Every other carrier is a claim about this repository
    and is true the moment the commit is made; this one is a claim about an
    external registry, so it is moved by `scripts/promote-image.py` in a
    separate commit, only after that script has proven the tag is really there
    (AUD-546). `scripts/release.sh` deliberately leaves it alone.

    This used to assert `pin == VERSION` outright, to stop a stale number
    handing new users an older release than the repository they cloned. That
    turned out to be the weaker of two competing goods: publishing is two steps
    (cut the tag, then dispatch `release.yml`), only the second creates the
    image, and the second needs a human in the GitHub UI. Between them the pin
    named a tag that did not exist and a fresh clone died at `docker compose up
    -d` with `manifest unknown`; v0.1.2 sat that way for nine days (AUD-442). An
    install that cannot start at all is worse than one running the previous
    release, so the rule is now **the pin may trail VERSION, never lead it**
    (CEO ruling, AUD-545).

    Trailing is bounded on both sides: it can never exceed VERSION (the
    `manifest unknown` case this replaces), and `promote-image.py` closes the
    gap as soon as the publish has happened. Note what is deliberately *not*
    asserted here — that the tag is really in GHCR. That is the property
    actually worth having, but it needs a network call, and a unit suite that
    goes red when the registry is unreachable reports a broken registry as a
    broken build. This is the offline
    half; `scripts/promote-image.py` owns the online one, and is the only thing
    that writes this line.
    """
    compose = (REPO_ROOT / "compose.yaml").read_text()
    tags = re.findall(
        r"^x-backend-image: &backend_image ghcr\.io/br3d/audr-backend:(\S+)$",
        compose,
        flags=re.M,
    )
    assert tags, (
        "compose.yaml has no `x-backend-image: &backend_image "
        "ghcr.io/br3d/audr-backend:<version>` line — scripts/promote-image.py "
        "rewrites exactly that spelling."
    )
    assert len(set(tags)) == 1, (
        f"compose.yaml names more than one backend tag ({sorted(set(tags))}); "
        "the three backend services must share one `x-backend-image` anchor."
    )
    pinned = tags[0]
    # A release tag, not a floating or sha-suffixed one: a clone has to pull
    # something whose meaning is fixed. Stricter than SEMVER_RE on purpose —
    # `<version>-g<sha12>` builds exist in the registry but are not releases.
    assert re.match(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$", pinned), (
        f"compose.yaml pins audr-backend:{pinned!r}, which is not a plain "
        "MAJOR.MINOR.PATCH release tag."
    )
    pinned_parts = tuple(int(n) for n in pinned.split("."))
    # Compare on the semver core only: SEMVER_RE admits `-rc.1`/`+build`
    # suffixes, and a pre-release VERSION must not make this raise ValueError.
    version_core = re.split(r"[-+]", __version__, maxsplit=1)[0]
    version_parts = tuple(int(n) for n in version_core.split("."))
    assert pinned_parts <= version_parts, (
        f"compose.yaml pins audr-backend:{pinned}, which is ahead of VERSION "
        f"({__version__}). A pin may trail VERSION while a release is cut but "
        "not yet published, never lead it — leading it means a fresh clone "
        "pulls a tag that does not exist and `docker compose up -d` fails with "
        "`manifest unknown` (AUD-442/AUD-545)."
    )
    assert "${BACKEND_TAG" not in compose and "${AUDR_REGISTRY" not in compose, (
        "BACKEND_TAG/AUDR_REGISTRY belong to compose.deploy.yaml — a plain "
        "install must not need either (AUD-439)."
    )


def test_build_info_defaults_to_empty_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dev checkout has no CI-injected build args; the version alone stands."""
    monkeypatch.delenv("AUDR_GIT_SHA", raising=False)
    monkeypatch.delenv("AUDR_BUILT_AT", raising=False)
    info = get_build_info()
    assert info.version == __version__
    assert info.commit == ""
    assert info.built_at == ""


def test_build_info_reads_injected_provenance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUDR_GIT_SHA", "deadbeefcafe")
    monkeypatch.setenv("AUDR_BUILT_AT", "2026-10-03T12:00:00Z")
    info = get_build_info()
    assert info.commit == "deadbeefcafe"
    assert info.built_at == "2026-10-03T12:00:00Z"


def test_version_endpoint_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    """No session required — the deploy health-gate calls this before login."""
    monkeypatch.setenv("AUDR_GIT_SHA", "0123456789ab")
    monkeypatch.setenv("AUDR_BUILT_AT", "2026-10-03T12:00:00Z")

    app = FastAPI()
    app.include_router(version_router)
    with TestClient(app) as client:
        response = client.get("/api/v1/version")

    assert response.status_code == 200
    assert response.json() == {
        "version": __version__,
        "commit": "0123456789ab",
        "built_at": "2026-10-03T12:00:00Z",
    }
