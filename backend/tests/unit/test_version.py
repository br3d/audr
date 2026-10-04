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


def test_pyproject_matches() -> None:
    data = tomllib.loads((REPO_ROOT / "backend/pyproject.toml").read_text())
    assert data["project"]["version"] == __version__


def test_package_json_matches() -> None:
    data = json.loads((REPO_ROOT / "frontend/package.json").read_text())
    assert data["version"] == __version__, (
        "frontend/package.json drives the version baked into the SPA bundle "
        "(frontend/build-meta.ts), so the sidebar would show the wrong number."
    )


def test_compose_default_image_tag_matches() -> None:
    """`docker compose up -d` on a checkout must pull that checkout's version.

    compose.yaml names the published image literally (AUD-418) so an operator
    can see what will be downloaded, which makes it a fifth version carrier:
    scripts/release.sh rewrites the `BACKEND_TAG` defaults and the header
    comment, and a stale number here would hand new users an older release
    than the repository they cloned.
    """
    compose = (REPO_ROOT / "compose.yaml").read_text()
    tags = re.findall(r"\$\{BACKEND_TAG:-([^}]+)\}", compose)
    assert tags, "compose.yaml has no ${BACKEND_TAG:-<version>} default"
    assert set(tags) == {__version__}, (
        f"compose.yaml defaults to audr-backend:{sorted(set(tags))}, "
        f"audr.version says {__version__} — bump with scripts/release.sh."
    )
    assert f"ghcr.io/br3d/audr-backend:{__version__}" in compose, (
        "the header comment in compose.yaml names a different version than the "
        "image lines below it."
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
