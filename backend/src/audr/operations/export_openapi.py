"""Static OpenAPI export for frontend type codegen: `python -m audr.operations.export_openapi`
(AUD-694).

`GET /openapi.json` is deliberately curated down to token-allowed GET
routes (see `audr.api.openapi_curated`) — it is a public, unauthenticated
endpoint and must not describe the session-only operator surfaces. The
frontend's generated types need the *full* schema FastAPI already builds
from every router's `response_model=`/request body, including those
operator-only routes, so this writes `app.openapi()` unfiltered.

Importing `audr.api.app` builds the `FastAPI` instance and walks its
routes for schema generation, but does not open a database connection or
require `DATABASE_URL`/`SECRET_KEY` — nothing here calls a route handler.
That is what keeps this usable from CI without booting the app or a
database (AUD-694).

Usage::

    python -m audr.operations.export_openapi [output_path]

Writes to *output_path* if given, else stdout. Exit 0 always on success;
import errors propagate as a normal traceback.
"""

from __future__ import annotations

import json
import sys


def export_openapi_json() -> str:
    """Return the full (uncurated) OpenAPI document as pretty-printed JSON."""
    from audr.api.app import app

    return json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n"


def _main() -> None:
    document = export_openapi_json()
    if len(sys.argv) > 2:
        print("usage: python -m audr.operations.export_openapi [output_path]", file=sys.stderr)
        raise SystemExit(2)
    if len(sys.argv) == 2:
        with open(sys.argv[1], "w") as f:
            f.write(document)
    else:
        sys.stdout.write(document)


if __name__ == "__main__":
    _main()
