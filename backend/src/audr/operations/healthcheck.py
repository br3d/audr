"""Container health check: `python -m audr.operations.healthcheck` (AUD-439).

This exists so `compose.yaml` can say

    test: ["CMD", "python", "-m", "audr.operations.healthcheck"]

instead of embedding a seven-line Python program in YAML. The rules it has to
honour are not obvious, so they live here rather than in a comment an operator
has to read before they can read the service definition.

`compose.yaml` does not call it yet: that file pins a published tag, and the
published `0.1.1` was cut the day before this module landed, so the check died
with `ModuleNotFoundError` and a clean clone reported `api` as `unhealthy`. It
inlines an equivalent in the meantime and reverts to the command above once a
tag carrying this module is published (AUD-442) — `scripts/release.sh` does that
revert in the release commit, so it does not depend on anyone remembering. Keep
the two in step until then; `backend/tests/unit/test_healthcheck.py` checks both
that the inline copy still obeys the rules below and that the revert is still
wired up. The rules:

* **stdlib only, no `curl`.** `curl` is not in the runtime image and installing
  it would add an unpinned apt layer for one HTTP request the interpreter
  already there can make. See docs/third-party.md section 1.3.
* **Exceptions are caught and printed, never raised.** An escaping traceback
  writes ~2KB into `.State.Health.Log`, of which Docker keeps only the last 5
  entries at 4KB each — so one failure crowds out the useful history. One line
  ("[Errno 111] Connection refused") says the same thing.
* **`/health/live` only.** The readiness probe (`/health/ready`) touches the
  database and the master key; using it here would make the container unhealthy
  — and therefore restarted — for an outage it cannot fix by restarting.
* **The port is fixed at 8000.** Only the host side of the published port is
  configurable (`AUDR_HTTP_PORT`); inside the container the app always listens
  on 8000, which is also what the deploy health-gate talks to.

Exit code 0 means live, 1 means anything else.

## `--worker` mode (AUD-539)

The worker container runs no HTTP server, so it has nothing for the rules
above to probe. Instead `python -m audr.operations.healthcheck --worker`
reads `WORKER_HEARTBEAT_PATH`, a local file `audr.jobs.__main__`'s poll loop
rewrites with the current time every `WORKER_BEAT_INTERVAL_S` seconds — on
its own timer, independent of whatever job it may be running, so a scan that
legitimately takes minutes never reads as a dead loop. Exit 0 if that
timestamp is within `WORKER_STALE_SECONDS` (3x the beat interval); 1 if it is
older, missing, or unreadable.

This is deliberately a file, not a query against the `worker_status` table:
the lesson from `/health/ready` (readiness) is that a check tied to the
database goes unhealthy — and therefore restarts the container — for a DB
outage no restart can fix. A local file keeps "my own loop is beating"
answerable even while the database the worker talks to is down.
"""

from __future__ import annotations

import os
import sys
import time
import urllib.request
from pathlib import Path

URL = "http://localhost:8000/health/live"
TIMEOUT_SECONDS = 4.0

WORKER_HEARTBEAT_PATH = Path(
    # Under /app, not /tmp: /app is already chown'd to the runtime user in the
    # Dockerfile, and ruff's S108 flags predictable /tmp paths as a symlink-attack
    # surface — moot for a single-user container, but there is no reason to
    # trip the lint for a path with an equally good home.
    os.environ.get("AUDR_WORKER_HEARTBEAT_PATH", "/app/worker-heartbeat")
)
WORKER_BEAT_INTERVAL_S = 5.0
WORKER_STALE_SECONDS = WORKER_BEAT_INTERVAL_S * 3


def _check_api() -> int:
    try:
        with urllib.request.urlopen(URL, timeout=TIMEOUT_SECONDS) as response:
            status = response.status
    except Exception as exc:  # noqa: BLE001 - one readable line beats a traceback
        print(exc)
        return 1
    if status != 200:
        print(f"{URL} returned HTTP {status}")
        return 1
    return 0


def _check_worker() -> int:
    try:
        last_beat = float(WORKER_HEARTBEAT_PATH.read_text())
    except Exception as exc:  # noqa: BLE001 - one readable line beats a traceback
        print(exc)
        return 1
    age = time.time() - last_beat
    if age > WORKER_STALE_SECONDS:
        print(f"worker heartbeat is {age:.1f}s old, over the {WORKER_STALE_SECONDS:.0f}s limit")
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--worker" in argv:
        return _check_worker()
    return _check_api()


if __name__ == "__main__":
    sys.exit(main())
