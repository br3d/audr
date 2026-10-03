"""Build-version endpoint (AUD-407).

`GET /api/v1/version` reports the semantic version of the running build plus,
when the image was built by CI, the commit it was built from and the build
timestamp.

Deliberately **unauthenticated**, unlike every other `/api/v1` route. The deploy
pipeline's health-gate and `scripts/smoke-test.sh` need to assert *which build*
answered on :80 before anyone has a session, and that assertion is the whole
point of tagging images with a semantic version. The response contains no
secrets and no per-user data — only what the image already announces by its own
registry tag.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from audr.version import get_build_info

router = APIRouter(prefix="/api/v1")


class VersionOut(BaseModel):
    version: str
    # Empty in a dev checkout — only CI builds inject these.
    commit: str
    built_at: str


@router.get("/version", response_model=VersionOut)
async def get_version() -> VersionOut:
    info = get_build_info()
    return VersionOut(version=info.version, commit=info.commit, built_at=info.built_at)
