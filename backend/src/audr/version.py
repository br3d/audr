"""Application version and build metadata (AUD-407).

`__version__` is the semantic version of the whole product — backend, worker and
SPA ship as one image, so there is one version number, not three. It is a
literal rather than a lookup because the runtime image installs no `audr`
distribution (the Dockerfile copies `backend/src` onto PYTHONPATH), so
`importlib.metadata.version("audr")` raises there, and the repo-root `VERSION`
file is not copied into the image either.

The literal is therefore duplicated in four places on purpose:

    VERSION                 — the human/script-facing source of truth
    backend/pyproject.toml  — [project].version
    frontend/package.json   — version (baked into the SPA bundle by Vite)
    this module             — what the API reports at runtime

`scripts/release.sh` rewrites all four together and
`backend/tests/test_version.py` fails the build if they ever drift.

`commit` and `built_at` are *build* metadata, not source: they are injected as
environment variables by the Dockerfile (`--build-arg GIT_SHA=... BUILT_AT=...`)
and are empty in a dev checkout, where the version alone is enough.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

__version__ = "0.1.0"


@dataclass(frozen=True)
class BuildInfo:
    """What the running process can say about its own build."""

    version: str
    commit: str
    built_at: str


def get_build_info() -> BuildInfo:
    """Return the running build's version and provenance.

    Reads the environment on every call rather than at import time so tests can
    monkeypatch the variables without reloading the module.
    """
    return BuildInfo(
        version=__version__,
        commit=os.environ.get("AUDR_GIT_SHA", ""),
        built_at=os.environ.get("AUDR_BUILT_AT", ""),
    )
