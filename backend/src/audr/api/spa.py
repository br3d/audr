"""Serving the built single-page app out of the API process (AUD-388).

Until AUD-388 the SPA was served by a separate `web` container running nginx,
which also reverse-proxied `/api/*` and `/health/*` to this API. That container
and the second `audr-frontend` image are gone; FastAPI now serves the bundle
that the `runtime` Dockerfile stage has always copied into `/app/static` and
nothing read.

Two behaviours the old nginx config provided have to be reproduced here, and
one deliberately must not be:

* `try_files $uri $uri/ /index.html` — a hard refresh on a client-side route
  such as `/folio` has no corresponding file on disk and must still return
  `index.html` so the SPA router can take over. See `_index_fallback`.
* `location ~ ^/(api|health)/` won before the SPA fallback, so an unknown API
  path produced the API's own 404 rather than the SPA shell. A Starlette
  ``Mount("/")`` matches *everything* that no earlier route claimed, so the
  guard has to be explicit — see `_RESERVED_PREFIXES`.
* nginx set `X-Forwarded-For` / `X-Forwarded-Proto` / `X-Real-IP` on the
  proxied requests. Nothing in the backend reads them (verified by grep over
  `backend/src` for AUD-388), so there is nothing to replace. If a
  TLS-terminating proxy is ever put in front of this, uvicorn's
  `--proxy-headers` is the knob, not application code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

logger = logging.getLogger(__name__)

# Paths the SPA must never answer for, so that an unknown `/api/v1/...` keeps
# returning the API's JSON error envelope instead of a 200 full of HTML. These
# are matched against the *un-stripped* request path: a `Mount("/")` contributes
# an empty prefix, so `scope["path"]` is still the full path inside the mount.
_RESERVED_PREFIXES = ("/api", "/health", "/openapi.json")

# Requests under the Vite asset directory are for content-hashed bundles. A miss
# there means the client is asking for a bundle this build does not contain —
# usually a stale index.html held by a browser or CDN across a deploy. Answering
# those with index.html would hand back HTML where the browser expects
# JavaScript or CSS, which surfaces as an opaque syntax error in the console
# instead of the 404 that actually explains the problem.
_NO_FALLBACK_PREFIXES = ("assets/",)

_INDEX = "index.html"


def _is_reserved(path: str) -> bool:
    return any(path == p or path.startswith(f"{p}/") for p in _RESERVED_PREFIXES)


class SpaStaticFiles(StaticFiles):
    """`StaticFiles` plus an `index.html` fallback for client-side routes."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Reject reserved paths here rather than in `get_response`, because
        # `StaticFiles.__call__` raises 405 for anything that is not GET/HEAD
        # before `get_response` is ever reached — which would turn a
        # `POST /api/v1/does-not-exist` into a 405 instead of the API's 404.
        if _is_reserved(scope.get("path", "")):
            raise StarletteHTTPException(status_code=404)
        await super().__call__(scope, receive, send)

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            return await self._index_fallback(path, scope)
        # Defensive: `html=True` makes StaticFiles *return* a 404 response in
        # some branches rather than raise, so cover both shapes.
        if response.status_code == 404:
            return await self._index_fallback(path, scope)
        return response

    async def _index_fallback(self, path: str, scope: Scope) -> Response:
        if path.startswith(_NO_FALLBACK_PREFIXES):
            raise StarletteHTTPException(status_code=404)
        # Not recursive: a missing index.html raises straight out of here,
        # which is the right answer for an image built without a frontend.
        return await super().get_response(_INDEX, scope)


def mount_spa(app: FastAPI, directory: Path | str) -> bool:
    """Mount the SPA at `/`, after every API router.

    Returns whether the mount happened. Mounting is skipped when the directory
    does not exist, which is the normal case in a dev checkout and in the test
    images: there is no build output to serve, and `StaticFiles` would
    otherwise raise at construction time — i.e. at import, since `create_app()`
    runs at module level.

    Note that the log lines below are effectively invisible in the API process:
    nothing in it calls `logging.basicConfig` (only `audr.jobs.__main__` does),
    so records below WARNING are dropped. Do not rely on them as the signal
    that an image shipped without a frontend — the deploy health-gate checks
    for HTTP 200 on `/` precisely because that is observable and this is not.
    """
    directory = Path(directory)
    if not directory.is_dir():
        logger.info("SPA static dir %s not present — not serving the SPA", directory)
        return False
    if not (directory / _INDEX).is_file():
        logger.warning("SPA static dir %s has no %s — not serving the SPA", directory, _INDEX)
        return False

    # Registered last so every API router matches first; `Mount("/")` is a
    # catch-all for whatever is left.
    app.mount("/", SpaStaticFiles(directory=str(directory), html=True), name="spa")
    logger.info("serving the SPA from %s", directory)
    return True
